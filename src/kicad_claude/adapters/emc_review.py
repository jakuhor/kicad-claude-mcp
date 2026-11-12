"""EMC pre-compliance review of a `.kicad_pcb` (OP-1).

Rule ideas and the published rules of thumb (λ/20 stitching, 3H spacing,
1 ground pin per 4 signal pins …) follow kicad-happy 3.0.1 `emc_rules.py`
(MIT License, Copyright (c) 2025 Andrew Klofas); the geometry is our own.
Where kicad-happy approximates a zone by its bounding box, this module tests
the real filled copper: every zone's filled polygons are rasterised onto one
grid (even-odd scanline, so KiCAD's fractured polygons with holes come out
right) and all checks query that grid.

A copper layer's *role* comes from its fill, not its position: a layer whose
ground fill covers >= `PLANE_FRACTION` of the board (`PLANE_FRACTION_ROUTED`
when it also carries signal tracks) is a ground plane, also when it is an
inner layer (kicad-happy reported Firbox's In1/In2 GND planes
as signal layers).

Everything here is a first-pass estimator for pre-compliance, not a
sign-off; each finding says what it measured.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from kicad_claude.adapters import pcb_editor, pcb_netlist
from kicad_claude.adapters.pcb_geometry import (
    board_outline_loops,
    is_ground_net,
    zone_layer_polygons,
)
from kicad_claude.adapters.sch_io import find_child, find_children

C0 = 299_792_458.0
PLANE_FRACTION = 0.5          # fill share that makes a layer without signals a plane
PLANE_FRACTION_ROUTED = 0.8   # ... and a layer that also carries signal tracks
POUR_FRACTION = 0.2           # fill share reported as a pour on a signal layer
POWER_NET_RE = re.compile(r"^([+-]\d|\+?V(CC|DD|BUS|IN|BAT|DC)(\b|_)|.*\d+V\d*(_\w+)?$)", re.I)
CLOCK_RE = re.compile(r"(CLK|SCK|SCLK|MCLK|BCLK|LRCK|LRCLK|WS|XTAL|OSC|XIN|XOUT)", re.I)
HIGH_SPEED_RE = re.compile(r"(USB|D[+-]$|DP$|DM$|_P$|_N$|HDMI|LVDS|ETH|RGMII|RMII|MDI|"
                           r"SDIO|QSPI|SPDIF|AES|I2S|TDM|SAI)", re.I)
CONNECTOR_PREFIXES = ("J", "P", "CN", "X", "USB")


def leaf(net: str) -> str:
    return (net or "").rsplit("/", 1)[-1]


def is_power_net(net: str) -> bool:
    return bool(net) and bool(POWER_NET_RE.search(leaf(net))) and not is_ground_net(net)


def is_signal_net(net: str) -> bool:
    return bool(net) and not is_ground_net(net) and not is_power_net(net) \
        and not leaf(net).startswith("unconnected-")


def lambda_over_20_mm(f_hz: float, eps_eff: float) -> float:
    return C0 / (f_hz * math.sqrt(eps_eff)) / 20.0 * 1000.0


# ---- raster grid ----------------------------------------------------------- #


@dataclass
class Grid:
    x0: float
    y0: float
    res: float
    nx: int
    ny: int

    def cell(self, x: float, y: float) -> tuple[int, int] | None:
        i, j = int((x - self.x0) / self.res), int((y - self.y0) / self.res)
        if 0 <= i < self.nx and 0 <= j < self.ny:
            return i, j
        return None

    def center(self, i: int, j: int) -> tuple[float, float]:
        return self.x0 + (i + 0.5) * self.res, self.y0 + (j + 0.5) * self.res

    def raster(self, polygons: list[list[tuple[float, float]]]) -> bytearray:
        """Even-odd fill of all `polygons` together, sampled at cell centres."""
        out = bytearray(self.nx * self.ny)
        edges = []
        for poly in polygons:
            n = len(poly)
            for k in range(n):
                (xa, ya), (xb, yb) = poly[k], poly[(k + 1) % n]
                if ya != yb:
                    edges.append((xa, ya, xb, yb))
        if not edges:
            return out
        # bucket edges by row span for speed
        rows: dict[int, list] = defaultdict(list)
        for e in edges:
            lo, hi = min(e[1], e[3]), max(e[1], e[3])
            j0 = max(0, int((lo - self.y0) / self.res - 0.5))
            j1 = min(self.ny - 1, int((hi - self.y0) / self.res + 0.5))
            for j in range(j0, j1 + 1):
                rows[j].append(e)
        for j, es in rows.items():
            y = self.y0 + (j + 0.5) * self.res
            xs = []
            for xa, ya, xb, yb in es:
                if (ya <= y < yb) or (yb <= y < ya):
                    xs.append(xa + (y - ya) * (xb - xa) / (yb - ya))
            xs.sort()
            base = j * self.nx
            for k in range(0, len(xs) - 1, 2):
                i0 = max(0, math.ceil((xs[k] - self.x0) / self.res - 0.5))
                i1 = min(self.nx - 1, math.floor((xs[k + 1] - self.x0) / self.res - 0.5))
                for i in range(i0, i1 + 1):
                    out[base + i] = 1
        return out

    def get(self, mask: bytearray, x: float, y: float) -> bool:
        c = self.cell(x, y)
        return bool(c) and bool(mask[c[1] * self.nx + c[0]])


def components(mask: bytearray, nx: int, ny: int) -> list[list[int]]:
    """4-connected components of a mask: lists of flat cell indices."""
    seen = bytearray(len(mask))
    out = []
    for start in range(len(mask)):
        if not mask[start] or seen[start]:
            continue
        stack, comp = [start], []
        seen[start] = 1
        while stack:
            c = stack.pop()
            comp.append(c)
            i, j = c % nx, c // nx
            for n in ((c - 1) if i > 0 else -1, (c + 1) if i < nx - 1 else -1,
                      (c - nx) if j > 0 else -1, (c + nx) if j < ny - 1 else -1):
                if n >= 0 and mask[n] and not seen[n]:
                    seen[n] = 1
                    stack.append(n)
        out.append(comp)
    return out


# ---- board model ------------------------------------------------------------ #


@dataclass
class Board:
    tree: list
    layers: list[str]
    dielectrics: list[dict]              # between layers[k] and layers[k+1]
    outline: list[list[tuple[float, float]]]
    grid: Grid
    board_mask: bytearray
    board_cells: int
    fill: dict[str, dict[str, bytearray]] = field(default_factory=dict)   # layer -> net -> mask
    tracks: list[dict] = field(default_factory=list)
    vias: list[dict] = field(default_factory=list)
    pads: list[dict] = field(default_factory=list)
    footprints: list[dict] = field(default_factory=list)
    unfilled_zones: int = 0

    def fraction(self, mask: bytearray) -> float:
        return sum(1 for a, b in zip(mask, self.board_mask) if a and b) / max(1, self.board_cells)

    def ground_mask(self, layer: str) -> bytearray:
        out = bytearray(len(self.board_mask))
        for net, m in self.fill.get(layer, {}).items():
            if is_ground_net(net):
                for k, v in enumerate(m):
                    if v:
                        out[k] = 1
        return out

    def net_at(self, layer: str, x: float, y: float) -> str | None:
        c = self.grid.cell(x, y)
        if not c:
            return None
        k = c[1] * self.grid.nx + c[0]
        for net, m in self.fill.get(layer, {}).items():
            if m[k]:
                return net
        return None

    def eps_r(self) -> float:
        vals = [d["epsilon_r"] for d in self.dielectrics if d.get("epsilon_r")]
        return sum(vals) / len(vals) if vals else 4.5


def _stackup(tree: list, layers: list[str]) -> list[dict]:
    setup = find_child(tree, "setup")
    st = find_child(setup, "stackup") if setup else None
    if not st:
        return []
    seq = []
    for node in find_children(st, "layer"):
        name = str(node[1])
        typ = find_child(node, "type")
        th = find_child(node, "thickness")
        er = find_child(node, "epsilon_r")
        seq.append({"name": name, "type": str(typ[1]) if typ else "",
                    "thickness": float(th[1]) if th and len(th) > 1 else None,
                    "epsilon_r": float(er[1]) if er and len(er) > 1 else None})
    out = []
    for a, b in zip(layers, layers[1:]):
        names = [s["name"] for s in seq]
        if a in names and b in names:
            between = seq[names.index(a) + 1: names.index(b)]
            th = sum(s["thickness"] or 0 for s in between if s["type"] in ("core", "prepreg"))
            ers = [s["epsilon_r"] for s in between if s["epsilon_r"]]
            out.append({"between": [a, b], "thickness_mm": round(th, 4) if th else None,
                        "epsilon_r": sum(ers) / len(ers) if ers else None})
        else:
            out.append({"between": [a, b], "thickness_mm": None, "epsilon_r": None})
    return out


def _footprints(tree: list) -> list[dict]:
    out = []
    for fp in pcb_editor.iter_footprints(tree):
        at = find_child(fp, "at")
        lib = str(fp[1]) if len(fp) > 1 and isinstance(fp[1], str) else ""
        layer = find_child(fp, "layer")
        props = {}
        for p in find_children(fp, "property"):
            if len(p) >= 3:
                props[str(p[1])] = str(p[2])
        out.append({"ref": pcb_editor.get_footprint_reference(fp) or "?",
                    "value": props.get("Value") or pcb_editor.get_fp_text(fp, "value") or "",
                    "lib": lib, "fields": props,
                    "x": float(at[1]) if at else 0.0, "y": float(at[2]) if at else 0.0,
                    "side": str(layer[1]) if layer else "F.Cu",
                    "dnp": find_child(fp, "dnp") is not None
                    or any(str(a) == "dnp" for a in (find_child(fp, "attr") or [])[1:])})
    return out


def load_board(tree: list, resolution_mm: float = 0.5) -> Board:
    layers = pcb_netlist.copper_layers(tree)
    loops = board_outline_loops(tree)
    if not loops:
        raise ValueError("board has no closed Edge.Cuts outline")
    xs = [x for lp in loops for x, _ in lp]
    ys = [y for lp in loops for _, y in lp]
    grid = Grid(min(xs), min(ys), resolution_mm,
                int(math.ceil((max(xs) - min(xs)) / resolution_mm)) + 1,
                int(math.ceil((max(ys) - min(ys)) / resolution_mm)) + 1)
    board_mask = grid.raster(loops)
    b = Board(tree=tree, layers=layers, dielectrics=_stackup(tree, layers), outline=loops,
              grid=grid, board_mask=board_mask, board_cells=sum(board_mask))
    polys: dict[tuple[str, str], list] = defaultdict(list)
    for z in zone_layer_polygons(tree, lambda node: pcb_editor.net_of(node, tree)):
        if not z["filled"]:
            b.unfilled_zones += 1
            continue
        if z["net"]:
            polys[(z["layer"], z["net"])].append(z["polygon"])
    for (layer, net), ps in polys.items():
        b.fill.setdefault(layer, {})[net] = grid.raster(ps)
    b.tracks = pcb_netlist._tracks(tree)
    b.vias = pcb_netlist._vias(tree, layers)
    b.pads = pcb_netlist.list_pads(tree)
    b.footprints = _footprints(tree)
    return b


def _f(sev: str, rule: str, msg: str, **kw) -> dict:
    return {"severity": sev, "rule": rule, "message": msg, **kw}


# ---- group: ground & stackup ------------------------------------------------ #


def layer_roles(b: Board) -> list[dict]:
    track_len: dict[str, float] = defaultdict(float)
    for t in b.tracks:
        if is_signal_net(t["net_name"]):
            track_len[t["layer"]] += math.dist(t["start"], t["end"])
    out = []
    for layer in b.layers:
        nets = {net: b.fraction(m) for net, m in b.fill.get(layer, {}).items()}
        gnd = b.fraction(b.ground_mask(layer))
        power = {n: round(f, 3) for n, f in nets.items() if not is_ground_net(n) and f > 0.02}
        top_power = max(power.items(), key=lambda kv: kv[1], default=(None, 0.0))
        need = PLANE_FRACTION_ROUTED if track_len[layer] >= 1 else PLANE_FRACTION
        if gnd >= need:
            role = "ground_plane"
        elif top_power[1] >= need:
            role = "power_plane"
        else:
            role = "signal"
        out.append({"layer": layer, "role": role, "ground_fill": round(gnd, 3),
                    "power_fill": power, "signal_track_mm": round(track_len[layer], 1),
                    "ground_nets": sorted(n for n in nets if is_ground_net(n))})
    return out


def check_ground_stackup(b: Board, findings: list, f_max_hz: float,
                         stitch_spacing_mm: float | None) -> dict:
    roles = layer_roles(b)
    role_of = {r["layer"]: r["role"] for r in roles}
    planes = [r["layer"] for r in roles if r["role"] != "signal"]

    any_ground = any(r["ground_fill"] > 0 for r in roles)
    if not any_ground:
        findings.append(_f("error", "EM-GND-NONE", "No filled ground copper on any layer "
                           "(zones unfilled or missing). Refill zones first if they exist."))

    # signal layers need an adjacent reference plane
    for k, r in enumerate(roles):
        if r["signal_track_mm"] < 1:      # any layer carrying signals, pour or not
            continue
        neigh = []
        if k > 0:
            neigh.append((b.layers[k - 1], b.dielectrics[k - 1] if k - 1 < len(b.dielectrics) else {}))
        if k + 1 < len(b.layers):
            neigh.append((b.layers[k + 1], b.dielectrics[k] if k < len(b.dielectrics) else {}))
        refs = [(n, d.get("thickness_mm")) for n, d in neigh if role_of.get(n) != "signal"
                and n not in (r["layer"],)]
        r["reference"] = [{"layer": n, "dielectric_mm": t} for n, t in refs]
        if not refs:
            findings.append(_f("warning", "EM-STACK-REF",
                               f"Signal layer {r['layer']} ({r['signal_track_mm']:.0f} mm of signal "
                               "track) has no adjacent plane layer: return currents spread and "
                               "loop area grows.", layer=r["layer"]))

    # ground islands: copper with no ground via / pad = floating antenna
    via_cells: dict[str, set] = defaultdict(set)
    for v in b.vias:
        if is_ground_net(v["net_name"]):
            c = b.grid.cell(*v["point"])
            if c:
                for layer in v["layers"]:
                    via_cells[layer].add(c[1] * b.grid.nx + c[0])
    for p in b.pads:
        if is_ground_net(p["net_name"]):
            c = b.grid.cell(*p["point"])
            if c:
                for layer in p["layers"]:
                    via_cells[layer].add(c[1] * b.grid.nx + c[0])
    islands = []
    cell_area = b.grid.res ** 2
    for layer in b.layers:
        for net, m in b.fill.get(layer, {}).items():
            if not is_ground_net(net):
                continue
            comps = components(m, b.grid.nx, b.grid.ny)
            for comp in comps:
                area = len(comp) * cell_area
                if area < 4 * cell_area:
                    continue
                anchored = any(c in via_cells[layer] for c in comp)
                if not anchored:
                    # look one cell around: via may sit on the island's rim
                    anchored = any((c + d) in via_cells[layer] for c in comp
                                   for d in (1, -1, b.grid.nx, -b.grid.nx))
                if not anchored:
                    i, j = comp[0] % b.grid.nx, comp[0] // b.grid.nx
                    x, y = b.grid.center(i, j)
                    islands.append({"layer": layer, "net": net, "area_mm2": round(area, 1),
                                    "at": [round(x, 2), round(y, 2)]})
    for isl in sorted(islands, key=lambda d: -d["area_mm2"])[:20]:
        findings.append(_f("warning", "EM-GND-ISLAND",
                           f"{isl['net']} copper island of {isl['area_mm2']} mm² on {isl['layer']} "
                           f"near ({isl['at'][0]}, {isl['at'][1]}) has no ground via or pad: "
                           "floating copper radiates. Stitch it or remove it.", **isl))

    # ground domains and where they join
    gnets = sorted({n for layer in b.fill for n in b.fill[layer] if is_ground_net(n)}
                   | {p["net_name"] for p in b.pads if is_ground_net(p["net_name"])})
    joins = []
    if len(gnets) > 1:
        by_ref: dict[str, set] = defaultdict(set)
        for p in b.pads:
            if is_ground_net(p["net_name"]):
                by_ref[p["ref"]].add(p["net_name"])
        val = {fp["ref"]: fp["value"] for fp in b.footprints}
        joins = [{"ref": r, "value": val.get(r, ""), "ground_nets": sorted(ns)}
                 for r, ns in sorted(by_ref.items()) if len(ns) > 1]
        findings.append(_f("info", "EM-GND-DOMAINS",
                           f"{len(gnets)} ground nets ({', '.join(gnets)}); parts joining them: "
                           + (", ".join(f"{j['ref']} {j['value']}" for j in joins) or "none")
                           + ". Check the join is the intended single point and that no "
                           "signal crosses the boundary (EM-SPLIT-CROSS).", ground_nets=gnets))

    # signal tracks over a gap in their reference plane
    gaps = _ref_gaps(b)
    for g in gaps[:30]:
        sev = "warning" if _fast(g["net"]) or g["longest_gap_mm"] >= REF_GAP_WARN_MM else "info"
        findings.append(_f(sev, "EM-REF-GAP",
                           f"Net {g['net']}: {g['gap_mm']} of {g['length_mm']} mm of track "
                           f"({100 - g['coverage_pct']:.0f} %) runs with no copper on its reference "
                           f"layer {g['reference']}; longest stretch {g['longest_gap_mm']} mm from "
                           f"({g['at'][0]}, {g['at'][1]}). The return current detours around "
                           "the gap: fill it or reroute.", net=g["net"],
                           gap_mm=g["gap_mm"], coverage_pct=g["coverage_pct"]))
    if len(gaps) > 30:
        findings.append(_f("info", "EM-REF-GAP", f"{len(gaps) - 30} more nets run over reference "
                           "gaps (see `ground.reference_gaps`)."))

    # signal crossing a change of reference-plane net
    crossings = _split_crossings(b, roles)
    for c in crossings:
        findings.append(_f("warning", "EM-SPLIT-CROSS",
                           f"Net {c['net']} on {c['layer']} crosses from {c['from']} to {c['to']} "
                           f"on reference {c['reference']} near ({c['at'][0]}, {c['at'][1]}) with no "
                           "capacitor joining the two within 5 mm: the return current has to "
                           "detour.", **c))

    # each signal layer change that moves the return current to another plane
    trans = _via_transitions(b, roles)
    bad = defaultdict(list)
    for t in trans:
        if not t["stitched"]:
            bad[t["net"]].append(t)

    def _sev(net, ts):
        return "warning" if ts[0]["kind"] == "signal" and _fast(net) else "info"
    order = sorted(bad.items(), key=lambda kv: (_sev(*kv) != "warning", kv[0]))
    for net, ts in order[:20]:
        n_all = sum(1 for t in trans if t["net"] == net)
        t0 = ts[0]
        what = (f"no {t0['stitch_net']} via reaching both planes within {t0['radius_mm']:g} mm"
                if t0["stitch"] == "via" else
                f"the planes are different nets ({' / '.join(t0['reference_nets'])}), so the "
                f"return crosses through a capacitor between them - none within "
                f"{t0['radius_mm']:g} mm")
        fix = (f"add a {t0['stitch_net']} via next to the {t0['kind']} via" if t0["stitch"] == "via"
               else "add a stitching capacitor between the two plane nets next to the via")
        findings.append(_f(_sev(net, ts), "EM-VIA-STITCH",
                           f"{t0['kind'].capitalize()} net {net}: {len(ts)} of {n_all} layer "
                           f"change(s) move the return current between "
                           f"{' / '.join(t0['reference_planes'])}; {what} (first at "
                           f"({t0['at'][0]}, {t0['at'][1]}), nearest {t0['nearest_mm']} mm): "
                           f"{fix}.", net=net, count=len(ts)))
    if len(bad) > 20:
        findings.append(_f("info", "EM-VIA-STITCH", f"{len(bad) - 20} more nets have unstitched "
                           "layer changes (see `ground.via_transitions`)."))

    # via stitching between ground planes / pours (λ/20 at f_max)
    eps = b.eps_r()
    spacing = stitch_spacing_mm or lambda_over_20_mm(f_max_hz, eps)
    stitch = _stitching(b, spacing)
    if stitch["cells"] and stitch["coverage"] < 0.9:
        worst = ", ".join(f"({x}, {y}) {d} mm" for x, y, d in stitch["worst"][:5])
        findings.append(_f("warning", "EM-STITCH",
                           f"Only {stitch['coverage']:.0%} of the area where ground copper overlaps "
                           f"on two or more layers lies within {spacing / 2:.1f} mm of a ground via "
                           f"(λ/20 = {spacing:.1f} mm at {f_max_hz / 1e6:.0f} MHz, εr {eps:.2f}). "
                           f"Worst spots: {worst}.", spacing_mm=round(spacing, 2)))

    # board edge: ground copper ring
    edge = _edge_coverage(b)
    if edge["samples"] and edge["coverage"] < 0.9:
        findings.append(_f("warning" if edge["coverage"] < 0.7 else "info", "EM-EDGE-GND",
                           f"Ground copper within 2 mm of the board edge on any layer covers "
                           f"{edge['coverage']:.0%} of the perimeter; uncovered edge lets plane "
                           "fields fringe out (edge radiation)."))

    # connectors: ground pins per signal pins
    conn = _connector_grounds(b, findings)

    return {"layers": roles, "planes": planes, "dielectrics": b.dielectrics,
            "ground_nets": gnets, "ground_joins": joins, "islands": islands,
            "split_crossings": crossings, "reference_gaps": gaps, "via_transitions": trans,
            "stitching": stitch,
            "edge": edge,
            "connectors": conn}


def _fast(net: str) -> bool:
    return bool(CLOCK_RE.search(leaf(net)) or HIGH_SPEED_RE.search(leaf(net)))


def _ref_layers(b: Board, roles: list[dict], layer: str) -> list[str]:
    k = b.layers.index(layer) if layer in b.layers else -1
    role_of = {r["layer"]: r["role"] for r in roles}
    out = []
    for n in (k - 1, k + 1):
        if 0 <= n < len(b.layers) and role_of.get(b.layers[n]) != "signal":
            out.append(b.layers[n])
    return out


def _split_crossings(b: Board, roles: list[dict]) -> list[dict]:
    caps = defaultdict(list)       # frozenset(nets) -> [(x, y)]
    by_ref = defaultdict(set)
    pos = {}
    for p in b.pads:
        by_ref[p["ref"]].add(p["net_name"])
        pos.setdefault(p["ref"], p["point"])
    for ref, nets in by_ref.items():
        if re.match(r"C\d", ref) and len(nets) == 2:
            caps[frozenset(nets)].append(pos[ref])
    out, seen = [], set()
    step = b.grid.res
    for t in b.tracks:
        if not is_signal_net(t["net_name"]):
            continue
        for ref in _ref_layers(b, roles, t["layer"]):
            (xa, ya), (xb, yb) = t["start"], t["end"]
            n = max(2, int(math.dist(t["start"], t["end"]) / step) + 1)
            prev = None
            for k in range(n):
                x, y = xa + (xb - xa) * k / (n - 1), ya + (yb - ya) * k / (n - 1)
                net = b.net_at(ref, x, y)
                if net is None:
                    continue
                if prev and net != prev[0]:
                    key = (t["net_name"], ref, prev[0], net)
                    bridged = any(math.dist((x, y), cp) <= 5.0
                                  for cp in caps.get(frozenset((prev[0], net)), []))
                    if not bridged and key not in seen:
                        seen.add(key)
                        out.append({"net": t["net_name"], "layer": t["layer"], "reference": ref,
                                    "from": prev[0], "to": net, "at": [round(x, 2), round(y, 2)]})
                prev = (net, x, y)
    return out


REF_GAP_MIN_MM = 1.0        # EM-REF-GAP: less than this per net is ignored
REF_GAP_WARN_MM = 5.0       # ... a gap this long is a warning on any signal


def _track_refs(b: Board) -> dict[str, list[str]]:
    """Per copper layer, the layers its tracks return on: the nearest
    adjacent layer that carries a pour anywhere; both neighbours only when
    their dielectrics are within 2x (a stripline), and when a thickness is
    unknown."""
    poured = {ly for ly, nets in b.fill.items() if nets}
    out = {}
    for k, ly in enumerate(b.layers):
        cand = []
        for n in (k - 1, k + 1):
            if 0 <= n < len(b.layers) and b.layers[n] in poured:
                d = b.dielectrics[min(k, n)] if min(k, n) < len(b.dielectrics) else {}
                cand.append((b.layers[n], d.get("thickness_mm")))
        th = [t for _, t in cand if t]
        if len(th) == len(cand) and th:
            cand = [c for c in cand if c[1] <= 2 * min(th)]
        out[ly] = [c[0] for c in cand]
    return out


def _ref_gaps(b: Board) -> list[dict]:
    """Signal track length with no copper under it on its reference layer(s),
    per net, sampled at the grid resolution. Points within 0.6 mm of a via or
    through-hole pad are skipped: a plane's antipad there is not a gap."""
    refs = _track_refs(b)
    holes = [v["point"] for v in b.vias] + [p["point"] for p in b.pads if len(p["layers"]) > 1]
    cell = 2.0
    bucket = defaultdict(list)
    for x, y in holes:
        bucket[(int(x // cell), int(y // cell))].append((x, y))

    def near_hole(x, y):
        bx, by = int(x // cell), int(y // cell)
        return any(math.dist((x, y), h) <= 0.6 for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                   for h in bucket.get((bx + dx, by + dy), []))
    per = defaultdict(lambda: {"length_mm": 0.0, "gap_mm": 0.0, "worst": None, "layers": set()})
    step = b.grid.res
    for t in b.tracks:
        net, rl = t["net_name"], refs.get(t["layer"]) or []
        if not is_signal_net(net) or not rl:
            continue
        (xa, ya), (xb, yb) = t["start"], t["end"]
        length = math.dist(t["start"], t["end"])
        n = max(1, int(length / step))
        rec = per[net]
        rec["length_mm"] += length
        run, run_at = 0.0, None
        for k in range(n):
            f = (k + 0.5) / n
            x, y = xa + (xb - xa) * f, ya + (yb - ya) * f
            if near_hole(x, y):
                continue
            if not any(b.net_at(r, x, y) for r in rl):
                rec["gap_mm"] += length / n
                rec["layers"].add(t["layer"])
                run += length / n
                run_at = run_at or (round(x, 2), round(y, 2))
            else:
                run, run_at = 0.0, None
            if run and (rec["worst"] is None or run > rec["worst"][0]):
                rec["worst"] = (round(run, 2), run_at, "/".join(rl))
    out = []
    for net, r in per.items():
        if r["gap_mm"] < REF_GAP_MIN_MM:
            continue
        out.append({"net": net, "length_mm": round(r["length_mm"], 1),
                    "gap_mm": round(r["gap_mm"], 1),
                    "coverage_pct": round(100 * (1 - r["gap_mm"] / r["length_mm"]), 1),
                    "longest_gap_mm": r["worst"][0], "at": list(r["worst"][1]),
                    "reference": r["worst"][2], "layers": sorted(r["layers"])})
    return sorted(out, key=lambda g: -g["gap_mm"])


def _plane_net_near(b: Board, layer: str, pt: tuple, r0: float) -> str | None:
    """The plane net around a via: the fill has an antipad at the via
    itself, so sample rings outside it (to 2 mm) and take the majority."""
    seen = defaultdict(int)
    for k in range(1, 5):
        rad = r0 + 0.4 * k
        for a in range(8):
            n = b.net_at(layer, pt[0] + rad * math.cos(a * math.pi / 4),
                         pt[1] + rad * math.sin(a * math.pi / 4))
            if n:
                seen[n] += 1
        if seen:
            return max(seen, key=seen.get)
    return None


def _via_transitions(b: Board, roles: list[dict]) -> list[dict]:
    """Signal and supply vias whose tracks leave on layers with different
    reference planes: the return current (for a supply, its AC part) must
    move between those planes at the via. Each layer's reference is the
    nearest adjacent pour at the via - both neighbours only when their
    dielectrics are within 2x (a stripline); a 2-layer board with pours on
    both sides counts.
    - Same plane net on both layers: a via (or through-hole pad) of that net
      spanning both plane layers within 2x the first dielectric, at least
      1 mm (kicad-happy RP-001), gives the return its short way.
    - Different plane nets (GND / +5V, GND / GND_USB): no via can join them;
      the return crosses through a capacitor between the two nets, so one
      within 5 mm (as EM-SPLIT-CROSS) is the stitch."""
    h = next((d["thickness_mm"] for d in b.dielectrics if d.get("thickness_mm")), 0.2)
    radius, cap_radius = max(2 * h, 1.0), 5.0
    joins = [(v["point"], v["net_name"], set(v["layers"])) for v in b.vias]
    joins += [(p["point"], p["net_name"], set(p["layers"])) for p in b.pads
              if len(p["layers"]) > 1]
    cap_nets, cap_pos = defaultdict(set), {}
    for p in b.pads:
        if re.match(r"C\d", p["ref"]):
            cap_nets[p["ref"]].add(p["net_name"])
            cap_pos.setdefault(p["ref"], p["point"])
    caps = [(cap_pos[r], frozenset(ns)) for r, ns in cap_nets.items() if len(ns) == 2]
    ends = defaultdict(list)                   # net -> [(point, layer)]
    for t in b.tracks:
        for pt in (t["start"], t["end"]):
            ends[t["net_name"]].append((pt, t["layer"]))
    out = []
    for v in b.vias:
        net = v["net_name"]
        if not net or is_ground_net(net) or leaf(net).startswith("unconnected-"):
            continue                            # a ground via is itself a stitch
        r = v.get("radius_mm", 0.3) + 0.05
        used = sorted({ly for pt, ly in ends.get(net, []) if math.dist(pt, v["point"]) <= r
                       and ly in v["layers"]})
        if len(used) < 2:
            continue
        refs = {}
        for ly in used:
            k = b.layers.index(ly)              # the adjacent pour at this spot, any role
            cand = []
            for n in (k - 1, k + 1):
                if 0 <= n < len(b.layers):
                    d = b.dielectrics[min(k, n)] if min(k, n) < len(b.dielectrics) else {}
                    cand.append((b.layers[n], _plane_net_near(b, b.layers[n], v["point"], r),
                                 d.get("thickness_mm")))
            cand = [c for c in cand if c[1]]
            # the return current crowds into the nearest plane: a neighbour
            # more than 2x farther is not the reference (In2 under a 1 mm core
            # with B.Cu 0.2 mm away refers to B.Cu); unknown thickness keeps both
            th = [t for _, _, t in cand if t]
            if len(th) == len(cand) and th:
                cand = [c for c in cand if c[2] <= 2 * min(th)]
            refs[ly] = [(rl, n) for rl, n, _ in cand]
        if any(not refs[ly] for ly in used) or any(
                {rl for rl, _ in refs[a]} & {rl for rl, _ in refs[b_]}
                for a in used for b_ in used if a < b_):
            continue                            # same plane on both sides, or none
        planes = sorted({rl for ly in used for rl, _ in refs[ly]})
        nets = sorted({n for ly in used for _, n in refs[ly]})
        sides = [{n for _, n in refs[ly]} for ly in used]
        common = set.intersection(*sides)
        rec = {"net": net, "kind": "signal" if is_signal_net(net) else "supply",
               "at": [round(v["point"][0], 2), round(v["point"][1], 2)], "layers": used,
               "reference_planes": planes, "reference_nets": nets}
        if common:
            # a via of the plane net that reaches every reference layer
            need = {rl for ly in used for rl, n in refs[ly] if n in common}
            d = min((math.dist(v["point"], pt) for pt, n, lys in joins
                     if n in common and need <= lys), default=math.inf)
            rec.update(stitch="via", stitch_net=sorted(common)[0], radius_mm=round(radius, 2),
                       nearest_mm=None if d == math.inf else round(d, 2), stitched=d <= radius)
        else:
            pairs = {frozenset((x, y)) for x in sides[0] for y in sides[1] if x != y}
            d = min((math.dist(v["point"], pt) for pt, ns in caps if ns in pairs),
                    default=math.inf)
            rec.update(stitch="capacitor", radius_mm=cap_radius,
                       nearest_mm=None if d == math.inf else round(d, 2), stitched=d <= cap_radius)
        out.append(rec)
    return out


def _stitching(b: Board, spacing: float) -> dict:
    gl = [b.ground_mask(layer) for layer in b.layers]
    vias = [v["point"] for v in b.vias if is_ground_net(v["net_name"])]
    vias += [p["point"] for p in b.pads
             if is_ground_net(p["net_name"]) and len(p["layers"]) > 1]
    if not vias:
        return {"cells": 0, "coverage": 0.0, "worst": [], "ground_vias": 0}
    # spatial hash of vias
    cell = max(spacing, 1.0)
    bucket = defaultdict(list)
    for x, y in vias:
        bucket[(int(x // cell), int(y // cell))].append((x, y))
    step = max(1, int(1.0 / b.grid.res))          # sample every ~1 mm
    total = ok = 0
    worst = []
    nx = b.grid.nx
    for j in range(0, b.grid.ny, step):
        for i in range(0, nx, step):
            k = j * nx + i
            if not b.board_mask[k] or sum(1 for m in gl if m[k]) < 2:
                continue
            x, y = b.grid.center(i, j)
            bx, by = int(x // cell), int(y // cell)
            d = min((math.dist((x, y), v) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                     for v in bucket.get((bx + dx, by + dy), [])), default=math.inf)
            total += 1
            if d <= spacing / 2:
                ok += 1
            else:
                worst.append((round(x, 1), round(y, 1), round(d, 1) if d != math.inf else None))
    worst.sort(key=lambda w: -(w[2] or 1e9))
    # keep spread-out worst points (>= spacing apart)
    picked = []
    for w in worst:
        if all(math.dist(w[:2], p[:2]) >= spacing for p in picked):
            picked.append(w)
        if len(picked) >= 10:
            break
    return {"cells": total, "coverage": ok / total if total else 1.0, "worst": picked,
            "ground_vias": len(vias), "spacing_mm": round(spacing, 2)}


def _edge_coverage(b: Board) -> dict:
    gl = [b.ground_mask(layer) for layer in b.layers]
    samples = covered = 0
    for loop in b.outline:
        n = len(loop)
        for k in range(n):
            (xa, ya), (xb, yb) = loop[k], loop[(k + 1) % n]
            length = math.dist((xa, ya), (xb, yb))
            m = max(1, int(length / 1.0))
            for s in range(m):
                t = (s + 0.5) / m
                x, y = xa + (xb - xa) * t, ya + (yb - ya) * t
                samples += 1
                hit = False
                for r in (0.5, 1.0, 1.5, 2.0):
                    for dx, dy in ((r, 0), (-r, 0), (0, r), (0, -r)):
                        if b.grid.get(b.board_mask, x + dx, y + dy) and \
                                any(b.grid.get(m_, x + dx, y + dy) for m_ in gl):
                            hit = True
                            break
                    if hit:
                        break
                covered += hit
    return {"samples": samples, "coverage": covered / samples if samples else 1.0}


def _connector_grounds(b: Board, findings: list) -> list[dict]:
    out = []
    by_ref = defaultdict(list)
    for p in b.pads:
        by_ref[p["ref"]].append(p)
    fp = {f["ref"]: f for f in b.footprints}
    for ref, pads in sorted(by_ref.items()):
        if not any(re.fullmatch(pre + r"\d+", ref) for pre in CONNECTOR_PREFIXES):
            continue
        nets = {}
        for p in pads:
            if p["net_name"] and not leaf(p["net_name"]).startswith("unconnected-"):
                nets[p["pad"]] = p["net_name"]
        gnd = sum(1 for n in nets.values() if is_ground_net(n))
        sig = sum(1 for n in nets.values() if is_signal_net(n))
        rec = {"ref": ref, "value": fp.get(ref, {}).get("value", ""), "ground_pins": gnd,
               "signal_pins": sig}
        out.append(rec)
        if sig <= 2:
            continue
        need = max(2, (sig + 3) // 4)
        if gnd < need:
            text = (rec["value"] + " " + fp.get(ref, {}).get("lib", "")).lower()
            hs = any(k in text for k in ("usb", "hdmi", "ethernet", "rj45", "pcie", "sata", "lvds"))
            findings.append(_f("warning" if hs else "info", "EM-CONN-GND",
                               f"{ref} ({rec['value']}) has {gnd} ground pin(s) for {sig} signal "
                               f"pins; rule of thumb is >= {need} (1 per 4, at least 2), spread "
                               "among the signals.", ref=ref))
    return out


# ---- group: switching converters --------------------------------------------- #

# Radiated emission limits (f_min MHz, f_max MHz, dBuV/m, distance m):
# FCC Part 15.109 (Class B at 3 m, Class A at 10 m), CISPR 32 / EN 55032
# (at 10 m). Same values as kicad-happy `emc_formulas.py`.
LIMITS = {
    "fcc-class-b": [(30, 88, 40.0, 3), (88, 216, 43.5, 3), (216, 960, 46.0, 3),
                    (960, 40000, 54.0, 3)],
    "fcc-class-a": [(30, 88, 39.1, 10), (88, 216, 43.5, 10), (216, 960, 46.4, 10),
                    (960, 40000, 49.5, 10)],
    "cispr-class-b": [(30, 230, 30.0, 10), (230, 1000, 37.0, 10)],
    "cispr-class-a": [(30, 230, 40.0, 10), (230, 1000, 47.0, 10)],
}
K_DM_GROUND = 2.632e-14   # Ott: E = K f^2 A I / r, with the ground-plane image


def emission_limit(f_hz: float, standard: str) -> tuple[float, float] | None:
    for lo, hi, lim, dist in LIMITS[standard]:
        if lo * 1e6 <= f_hz < hi * 1e6:
            return lim, dist
    return None


def trapezoid_harmonic(n: int, peak: float, duty: float, rise_s: float, fsw: float) -> float:
    """Ott, EMC Engineering ch. 2: |2 A D sinc(n pi D) sinc(n pi tr f)|."""
    x1 = n * math.pi * duty
    x2 = n * math.pi * rise_s * fsw
    s1 = math.sin(x1) / x1 if x1 else 1.0
    s2 = math.sin(x2) / x2 if x2 else 1.0
    return abs(2 * peak * duty * s1 * s2)


def _net_copper_area(b: Board, net: str) -> dict:
    tracks = sum(t["width_mm"] * math.dist(t["start"], t["end"])
                 for t in b.tracks if t["net_name"] == net)
    zones = 0.0
    for layer in b.layers:
        m = b.fill.get(layer, {}).get(net)
        if m:
            zones += sum(m) * b.grid.res ** 2
    return {"track_mm2": round(tracks, 1), "zone_mm2": round(zones, 1),
            "total_mm2": round(tracks + zones, 1)}


def _poly_area(pts: list) -> float:
    return abs(sum(x1 * y2 - x2 * y1
                   for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))) / 2


def _edge_distance(b: Board, x: float, y: float) -> float:
    best = math.inf
    for loop in b.outline:
        for (xa, ya), (xb, yb) in zip(loop, loop[1:] + loop[:1]):
            dx, dy = xb - xa, yb - ya
            L2 = dx * dx + dy * dy
            t = 0 if not L2 else max(0.0, min(1.0, ((x - xa) * dx + (y - ya) * dy) / L2))
            best = min(best, math.dist((x, y), (xa + t * dx, ya + t * dy)))
    return best


def _switch_nets(r: dict, nl, dc_nets: set) -> set:
    """SW pin net, or this channel's pin nets that reach an inductor; nets
    with a known DC voltage (`dc_nets`, the rails) are never switch nodes."""
    ref, ch = r["ref"], r["channel"] or ""
    nets = {r["switch_net"]} if r["switch_net"] else set()
    for p in nl.pins.get(ref, []):
        base = re.sub(r"\d+$", "", p.name or "")
        if (p.name or "")[len(base):] not in (ch, ""):
            continue
        if p.net in (r["input_net"], r["output_net"]) or is_ground_net(p.net) \
                or leaf(p.net).startswith("unconnected-"):
            continue
        if p.net in dc_nets:
            continue
        if any(q.ref.startswith("L") for q in nl.nets.get(p.net, []) if q.ref != ref):
            nets.add(p.net)
    nets.discard(None)
    return nets


def _input_loop(b: Board, ref: str, vin_net: str, pads_by_ref: dict) -> dict | None:
    """Loop input cap (+) -> IC VIN pad -> nearest IC ground pad -> cap (-),
    through the real pad positions, for the input cap nearest the VIN pad."""
    ic = pads_by_ref.get(ref, [])
    vin_pads = [p for p in ic if p["net_name"] == vin_net]
    gnd_pads = [p for p in ic if is_ground_net(p["net_name"])]
    if not vin_pads or not gnd_pads:
        return None
    best = None
    for cref, cps in pads_by_ref.items():
        if not re.match(r"C\d", cref) or len(cps) != 2:
            continue
        cv = [p for p in cps if p["net_name"] == vin_net]
        cg = [p for p in cps if is_ground_net(p["net_name"])]
        if not cv or not cg:
            continue
        for vp in vin_pads:
            d = math.dist(cv[0]["point"], vp["point"])
            if best is None or d < best[0]:
                best = (d, cref, cv[0], cg[0], vp)
    if not best:
        return None
    d, cref, cv, cg, vp = best
    gp = min(gnd_pads, key=lambda p: math.dist(p["point"], cg["point"]))
    area = _poly_area([cv["point"], vp["point"], gp["point"], cg["point"]])
    return {"cap": cref, "cap_to_vin_pin_mm": round(d, 2), "area_mm2": round(area, 1),
            "same_side": (cv["layers"][:1] == vp["layers"][:1])}


_RT_PIN = re.compile(r"^(RT|RT/SYNC|RT/CLK|FSW|FREQ|ROSC)$", re.I)
_SYNC_PIN = re.compile(r"SYNC|CLKIN|EXTCLK|^CLK$", re.I)


def fsw_from_strap(nl, ref: str, hint: dict | None) -> tuple[float, str] | None:
    """The datasheet fsw when the schematic meets its stated condition: the
    RT pin floating ("RT floating / open") or one resistor to ground of the
    stated value ("RT = 46.4k", within 1 %). Anything else stays the caller's."""
    if not hint or nl is None or not (hint.get("typ") or hint.get("max")):
        return None
    from kicad_claude.adapters.power_review import is_unconnected, kind, parse_value
    cond = hint.get("conditions") or ""
    pin = next((p for p in nl.pins.get(ref, []) if _RT_PIN.match((p.name or "").strip())), None)
    if pin is None:
        return None
    hz = float(hint.get("typ") or hint.get("max"))
    # a clock on SYNC overrides RT: the strap holds only with SYNC idle
    # (grounded, open, or reached only by test points and resistors to ground)
    for sp in nl.pins.get(ref, []):
        if sp is pin or not _SYNC_PIN.search((sp.name or "").strip()):
            continue
        if is_ground_net(sp.net) or is_unconnected(sp.net):
            continue
        for q in nl.nets.get(sp.net, []):
            if q.ref == ref or kind(q.ref) == "TP" or nl.components.get(q.ref, {}).get("dnp"):
                continue
            far = [x.net for x in nl.pins.get(q.ref, []) if x.net != sp.net]
            if not (kind(q.ref) == "R" and far and is_ground_net(far[0])):
                return None
    others = [q for q in nl.nets.get(pin.net, []) if q.ref != ref]
    if re.search(r"\b(float|floating|open|unconnected|NC)\b", cond, re.I):
        if is_unconnected(pin.net) or not others:
            return hz, f"RT pin {pin.name} floating, as the datasheet condition '{cond}'"
        return None
    m = re.search(r"R_?T\w*\s*=\s*([\d.]+\s*[kKM]?)", cond)
    want = parse_value(m.group(1).replace(" ", "")) if m else None
    res = [q for q in others if kind(q.ref) == "R" and not nl.components.get(q.ref, {}).get("dnp")]
    if not want or len(res) != 1 or len([q for q in others if kind(q.ref) != "TP"]) != 1:
        return None
    r = res[0]
    far = [p.net for p in nl.pins.get(r.ref, []) if p.net != pin.net]
    have = parse_value(nl.components.get(r.ref, {}).get("value", ""))
    if far and is_ground_net(far[0]) and have and abs(have - want) <= 0.01 * want:
        return hz, f"{r.ref} {nl.components[r.ref]['value']} on {pin.name} to ground matches '{cond}'"
    return None


def check_switching(b: Board, findings: list, regulators: list[dict], nl,
                    rail_volts: dict, fsw: dict, iout: dict, specs: dict,
                    standard: str, rise_time_s: float) -> list[dict]:
    """`regulators`: analyze_power_tree records; `nl`: its Netlist;
    `rail_volts`: net -> volts; `fsw` / `iout`: caller values by regulator
    name (`U2/ch1`) or ref; `specs`: {ref: spec record}, used for the load
    current fallback and as an fsw hint only."""
    pads_by_ref = defaultdict(list)
    for p in b.pads:
        pads_by_ref[p["ref"]].append(p)
    fp_pos = {f["ref"]: (f["x"], f["y"]) for f in b.footprints}
    conns = [f for f in b.footprints
             if any(re.fullmatch(pre + r"\d+", f["ref"]) for pre in CONNECTOR_PREFIXES)]
    out = []
    for r in regulators:
        if r["topology"] != "switching":
            continue
        name, ref = r["name"], r["ref"]
        sw_nets = _switch_nets(r, nl, set(rail_volts))
        rec = {"regulator": name, "value": r["value"], "switch_nets": sorted(sw_nets)}

        areas = {n: _net_copper_area(b, n) for n in sorted(sw_nets)}
        rec["switch_node_copper"] = areas
        for n, a in areas.items():
            if a["total_mm2"] >= 25:
                findings.append(_f("warning" if a["total_mm2"] > 100 else "info", "EM-SW-AREA",
                                   f"{name} switching net {n}: {a['total_mm2']} mm² of copper "
                                   f"(tracks {a['track_mm2']}, zones {a['zone_mm2']}; pads not "
                                   "counted). Switch-node copper is a dV/dt antenna - keep it to "
                                   "the pad-to-inductor connection.", regulator=name, net=n))

        loop = _input_loop(b, ref, r["input_net"], pads_by_ref)
        rec["input_loop"] = loop
        if loop and loop["cap_to_vin_pin_mm"] > 5:
            findings.append(_f("warning", "EM-SW-CIN",
                               f"{name}: nearest input capacitor {loop['cap']} is "
                               f"{loop['cap_to_vin_pin_mm']} mm from the VIN pin; the hot loop "
                               f"closes through {loop['area_mm2']} mm².", regulator=name))

        f = fsw.get(name, fsw.get(ref))
        hint = ((specs.get(ref) or {}).get("regulator") or {}).get("fsw")
        strap = None if f else fsw_from_strap(nl, ref, hint)
        if strap:
            f = strap[0]
            rec["fsw_source"] = f"datasheet {(specs.get(ref) or {}).get('mpn')}: {strap[1]}"
        elif f:
            rec["fsw_source"] = "caller"
        if not f:
            findings.append(_f("info", "EM-SW-FSW",
                               f"{name}: no switching frequency given (fsw={{'{name}': Hz}}); "
                               "harmonic and radiation estimates skipped."
                               + (f" Datasheet value {hint.get('typ') or hint.get('max')} Hz "
                                  f"holds only for '{hint.get('conditions', '')}' - check the "
                                  "RT/SYNC setting." if hint else ""), regulator=name))
        else:
            vin = rail_volts.get(r["input_net"])
            if vin is None:      # e.g. LT8471 ch2 fed from SHOUT: use the IC's other input
                vin = next((rail_volts[o["input_net"]] for o in regulators
                            if o["ref"] == ref and o["input_net"] in rail_volts), None)
            vout = rail_volts.get(r["output_net"])
            if vin and vout is not None:
                # buck only when the switch-node inductor lands on the output rail
                buck = bool(r.get("inductor")) and r["inductor"].get("other_net") == r["output_net"]                     and 0 < vout < vin
                duty = vout / vin if buck else abs(vout) / (abs(vout) + abs(vin))
                duty_src = "Vout/Vin (buck)" if buck else "|Vout|/(|Vout|+Vin) (inverting/Cuk/Zeta)"
            else:
                duty, duty_src = 0.5, "assumed 0.5 (rail voltages unknown)"
            duty = max(0.05, min(0.95, duty))
            env = {"fsw_hz": f, "duty": round(duty, 3), "duty_source": duty_src,
                   "rise_time_s": rise_time_s,
                   "corner_1_hz": round(f / (math.pi * duty)),
                   "corner_2_hz": round(1 / (math.pi * rise_time_s)),
                   "first_harmonic_from_150kHz": max(1, math.ceil(150e3 / f)),
                   "first_harmonic_from_30MHz": math.ceil(30e6 / f)}
            rec["harmonics"] = env
            i_load, i_src = iout.get(name, iout.get(ref)), "caller"
            if i_load is None:
                sv = ((specs.get(ref) or {}).get("recommended") or {}).get("IOUT")
                if sv and sv.get("max") is not None:
                    i_load = float(sv["max"])
                    i_src = f"datasheet IOUT max p.{sv.get('page')}, worst case"
            if not (loop and i_load):
                findings.append(_f("info", "EM-SW-RAD",
                                   f"{name}: radiation estimate skipped - "
                                   + ("no input loop found" if not loop else
                                      "no load current (pass iout={'" + name + "': A})") + ".",
                                   regulator=name))
            else:
                worst = None
                for n in range(env["first_harmonic_from_30MHz"], int(1e9 / f) + 1):
                    fn = n * f
                    lim = emission_limit(fn, standard)
                    if not lim:
                        continue
                    i_n = trapezoid_harmonic(n, i_load, duty, rise_time_s, f)
                    e = K_DM_GROUND * fn ** 2 * loop["area_mm2"] * 1e-6 * i_n / lim[1]
                    dbuv = 20 * math.log10(e * 1e6) if e > 0 else -999.0
                    if worst is None or lim[0] - dbuv < worst["margin_db"]:
                        worst = {"harmonic": n, "f_hz": round(fn), "e_dbuv_m": round(dbuv, 1),
                                 "limit_dbuv_m": lim[0], "distance_m": lim[1],
                                 "margin_db": round(lim[0] - dbuv, 1)}
                rec["radiation_estimate"] = {"standard": standard, "load_current_a": i_load,
                                             "load_source": i_src, "worst": worst}
                rec["envelope"] = {"fsw_hz": f, "duty": duty, "rise_time_s": rise_time_s,
                                   "load_current_a": i_load, "loop_area_mm2": loop["area_mm2"]}
                if worst:
                    findings.append(_f(
                        "warning" if worst["margin_db"] < 6 else "info", "EM-SW-RAD",
                        f"{name}: differential-mode estimate, {loop['area_mm2']} mm² input loop, "
                        f"{i_load} A ({i_src}), fsw {f / 1e6:.3g} MHz, duty {duty:.2f}, tr "
                        f"{rise_time_s * 1e9:.0f} ns: worst harmonic {worst['harmonic']} at "
                        f"{worst['f_hz'] / 1e6:.1f} MHz = {worst['e_dbuv_m']} dBµV/m vs "
                        f"{worst['limit_dbuv_m']} ({standard}, {worst['distance_m']} m), margin "
                        f"{worst['margin_db']} dB. First-pass estimator; common-mode (cable) "
                        "emission is not modelled.", regulator=name))

        inds = sorted({q.ref for n in sw_nets for q in nl.nets.get(n, []) if q.ref.startswith("L")})
        rec["inductors"] = []
        for lref in inds:
            if lref not in fp_pos:
                continue
            x, y = fp_pos[lref]
            de = _edge_distance(b, x, y)
            dc = min(((math.dist((x, y), (c["x"], c["y"])), c["ref"]) for c in conns),
                     default=(math.inf, None))
            rec["inductors"].append({"ref": lref, "edge_mm": round(de, 1),
                                     "nearest_connector": dc[1], "connector_mm": round(dc[0], 1)})
            if de < 3:
                findings.append(_f("warning", "EM-SW-L-EDGE",
                                   f"{name} switching inductor {lref} centre is {de:.1f} mm from "
                                   "the board edge: its stray field is not contained by the planes.",
                                   regulator=name, ref=lref))
            if dc[0] < 5:
                findings.append(_f("warning", "EM-SW-L-CONN",
                                   f"{name} switching inductor {lref} is {dc[0]:.1f} mm from "
                                   f"connector {dc[1]}: magnetic coupling into the cable.",
                                   regulator=name, ref=lref))
        out.append(rec)
    return out


# ---- group: decoupling & PDN ---------------------------------------------------- #

PDN_FREQS = (1e5, 1e6, 1e7, 1e8)          # rail impedance profile (Hz)


def _two_pin_parts(b: Board) -> dict[str, list[dict]]:
    by_ref = defaultdict(list)
    for p in b.pads:
        by_ref[p["ref"]].append(p)
    return {r: ps for r, ps in by_ref.items() if len(ps) == 2}


def _parse_cap(value: str) -> float | None:
    from kicad_claude.adapters.power_review import parse_value
    v = parse_value((value or "").split("/")[0])
    return v if v and v < 1 else None     # farads; "10u" -> 1e-5


def check_decoupling(b: Board, findings: list, nl, rail_volts: dict, specs: dict,
                     decap_max_mm: float, cap_via_max_mm: float,
                     pdn: dict) -> dict:
    """Supply pins (power_in, from the netlist) vs their nearest capacitor to
    ground on the same net; each cap's ground pad vs the nearest ground via;
    per-rail capacitor bank vs a caller target impedance."""
    caps = {}
    for ref, ps in _two_pin_parts(b).items():
        if not re.match(r"C\d", ref):
            continue
        nets = [p["net_name"] for p in ps]
        g = [p for p in ps if is_ground_net(p["net_name"])]
        s = [p for p in ps if not is_ground_net(p["net_name"])]
        if len(g) == 1 and len(s) == 1:
            caps[ref] = {"rail": s[0]["net_name"], "pad": s[0], "gnd_pad": g[0], "nets": nets}
    fp_val = {f["ref"]: f for f in b.footprints}
    pad_at = {(p["ref"], p["pad"]): p for p in b.pads}

    # 1. supply pin -> nearest cap on the same net
    pins = []
    if nl is not None:
        for ref, plist in nl.pins.items():
            if not re.match(r"(U|IC)\d", ref):
                continue
            for p in plist:
                if p.type.split("+")[0] != "power_in" or is_ground_net(p.net):
                    continue
                pad = pad_at.get((ref, p.number))
                if not pad:
                    continue
                cands = [(math.dist(pad["point"], c["pad"]["point"]), cr)
                         for cr, c in caps.items() if c["rail"] == p.net
                         and not fp_val.get(cr, {}).get("dnp")]
                d, cr = min(cands, default=(math.inf, None))
                rec = {"ref": ref, "pin": f"{p.name or p.number}({p.number})", "net": p.net,
                       "nearest_cap": cr, "distance_mm": round(d, 2) if cr else None}
                pins.append(rec)
        # one finding per IC and net (a BGA's 13 VDD_IO balls are one decision)
        groups = defaultdict(list)
        for rec in pins:
            groups[(rec["ref"], rec["net"])].append(rec)
        for (ref, net), rows in groups.items():
            names = ", ".join(r["pin"] for r in rows)
            if all(r["nearest_cap"] is None for r in rows):
                dnp = sorted(c for c, v in caps.items() if v["rail"] == net)
                findings.append(_f("warning", "EM-DEC-NONE",
                                   f"{ref} pin{'s' if len(rows) > 1 else ''} {names} on {net}: " + (
                                       f"only capacitors marked DNP on the board "
                                       f"({', '.join(dnp[:5])}{' …' if len(dnp) > 5 else ''}) "
                                       "- if they are fitted, the board's DNP flags are stale "
                                       "(update the PCB from the schematic)." if dnp else
                                       "no capacitor to ground on that net anywhere on the "
                                       "board."), ref=ref, net=net))
                continue
            far = [r for r in rows if r["distance_mm"] is not None
                   and r["distance_mm"] > decap_max_mm]
            if far:
                w = max(far, key=lambda r: r["distance_mm"])
                findings.append(_f("warning", "EM-DEC-DIST",
                                   f"{ref} pin{'s' if len(far) > 1 else ''} "
                                   f"{', '.join(r['pin'] for r in far)} on {net}: nearest "
                                   f"decoupling cap up to {w['distance_mm']:.1f} mm away ({w['pin']} "
                                   f"-> {w['nearest_cap']}; > {decap_max_mm} mm); its mounting "
                                   "inductance grows with the trace.", ref=ref, net=net))

    # 2. cap ground pad -> nearest ground via (path to the plane)
    gvias = [v["point"] for v in b.vias if is_ground_net(v["net_name"])]
    cap_vias = []
    for cr, c in sorted(caps.items(), key=lambda kv: _nat_ref(kv[0])):
        if fp_val.get(cr, {}).get("dnp"):
            continue
        d = min((math.dist(c["gnd_pad"]["point"], v) for v in gvias), default=math.inf)
        cap_vias.append({"ref": cr, "rail": c["rail"],
                         "gnd_via_mm": round(d, 2) if d != math.inf else None})
        if d > cap_via_max_mm and len(c["gnd_pad"]["layers"]) == 1:
            findings.append(_f("info", "EM-DEC-VIA",
                               f"{cr} ({fp_val.get(cr, {}).get('value', '')} on {c['rail']}): "
                               f"nearest ground via is "
                               + (f"{d:.1f} mm" if d != math.inf else "missing")
                               + f" from its ground pad (> {cap_via_max_mm} mm).", ref=cr))

    # 3. per-rail bank vs target impedance
    banks = {}
    for cr, c in caps.items():
        if fp_val.get(cr, {}).get("dnp"):
            continue
        val = _parse_cap(fp_val.get(cr, {}).get("value", ""))
        bank = banks.setdefault(c["rail"], {"count": 0, "c_total_f": 0.0, "esl_known": 0,
                                            "esr_known": 0, "caps": [], "_inv_esl": 0.0})
        bank["count"] += 1
        bank["caps"].append(cr)
        if val:
            from kicad_claude.adapters.datasheet_specs import c_bias_ratio
            v_rail = rail_volts.get(c["rail"])
            ratio = c_bias_ratio(specs.get(cr), v_rail, val) if v_rail is not None else None
            if ratio is not None:
                bank.setdefault("dc_bias_derated", []).append(cr)
            bank["c_total_f"] += val * (ratio if ratio is not None else 1.0)
        el = (specs.get(cr) or {}).get("electrical") or {}
        bank["esl_known"] += bool(el.get("ESL"))
        bank["esr_known"] += bool(el.get("ESR"))
        esl = (el.get("ESL") or {}).get("typ") or (el.get("ESL") or {}).get("max")
        if esl:
            bank["_inv_esl"] += 1.0 / esl
        esr = (el.get("ESR") or {}).get("typ") or (el.get("ESR") or {}).get("max")
        c_eff = (val * (ratio if val and ratio is not None else 1.0)) if val else None
        bank.setdefault("_branches", []).append((cr, c_eff, esr, esl))
    # rail impedance for every bank, target or not: each cap a series C-ESR-L
    # branch, L = datasheet ESL + the layout's mounting inductance (nothing assumed)
    mount = {}
    if banks:
        try:
            from kicad_claude.adapters import spice_auto
            mount = {c: v["l_h"] for c, v in spice_auto.mounting_inductance(
                b.tree, sorted({c for bk in banks.values() for c in bk["caps"]})).items()
                if v.get("l_h")}
        except (ValueError, KeyError):
            mount = {}
    for rail, bank in banks.items():
        br = bank.pop("_branches", [])
        if br and all(c for _, c, _, _ in br):
            prof = {}
            for f in PDN_FREQS:
                w = 2 * math.pi * f
                y = 0j
                for cr, c, esr, esl in br:
                    lh = (esl or 0.0) + mount.get(cr, 0.0)
                    y += 1 / complex(esr or 0.0, w * lh - 1 / (w * c))
                prof[f"{f:g}"] = float(f"{abs(1 / y):.3g}") if y else None
            bank["z_profile_ohm"] = prof
            bank["z_profile_basis"] = (
                f"C{' (DC-bias derated)' if bank.get('dc_bias_derated') else ''}; ESR from "
                f"{bank['esr_known']}/{bank['count']} datasheets (else 0); L = datasheet ESL "
                f"({bank['esl_known']}/{bank['count']}) + layout mounting inductance "
                f"({sum(1 for cr, *_ in br if cr in mount)}/{bank['count']}). Not included: "
                "plane capacitance (it dominates near 100 MHz) and spreading inductance; an "
                "unknown ESR (0) makes resonance dips too deep and anti-resonance peaks too high")
        inv = bank.pop("_inv_esl")
        bank["c_total_f"] = float(f"{bank['c_total_f']:.4g}")
        if inv and bank["esl_known"] == bank["count"]:
            bank["esl_parallel_h"] = float(f"{1.0 / inv:.3g}")
        bank["volts"] = rail_volts.get(rail)
        tgt = pdn.get(rail)
        if not tgt:
            continue
        if isinstance(tgt, dict):
            v = abs(rail_volts.get(rail) or 0)
            z = v * tgt["ripple"] / tgt["i_step"] if v else None
        else:
            z = float(tgt)
        bank["z_target_ohm"] = z
        if z and bank["c_total_f"]:
            f_lo = 1 / (2 * math.pi * bank["c_total_f"] * z)
            bank["meets_target_above_hz"] = round(f_lo)
            if bank.get("esl_parallel_h"):
                # inductive asymptote: |Z| = 2*pi*f*L_parallel (ESL only; the
                # mounting / plane inductance comes on top and lowers f_hi)
                f_hi = z / (2 * math.pi * bank["esl_parallel_h"])
                bank["meets_target_below_hz"] = round(f_hi)
                note = (f" Upper end {f_hi / 1e6:.3g} MHz from the caps' datasheet ESL in "
                        f"parallel ({bank['esl_parallel_h'] * 1e12:.0f} pH; mounting inductance "
                        "not included)."
                        + (" The band is empty: the bank cannot meet the target." if f_hi <= f_lo
                           else ""))
            else:
                note = (f" ESL known for {bank['esl_known']}/{bank['count']} caps (extract their "
                        "datasheets): the upper end of the band is not computed.")
            findings.append(_f("warning" if bank.get("meets_target_below_hz", math.inf) <= f_lo
                               else "info", "EM-PDN",
                               f"{rail}: {bank['count']} caps, {bank['c_total_f']:.3g} F total"
                               + (f" ({len(bank['dc_bias_derated'])} derated by their DC-bias "
                                  "curve)" if bank.get("dc_bias_derated") else "") + "; "
                               f"bank stays under {z:.3g} Ω only above {f_lo / 1e3:.3g} kHz "
                               "(capacitive asymptote)." + note, net=rail))
    return {"supply_pins": pins, "cap_vias": cap_vias, "banks": banks}


def _nat_ref(s: str) -> tuple:
    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s))


# ---- group: I/O, signals ------------------------------------------------------- #

TVS_RE = re.compile(r"TVS|ESD|SMF\d|SMAJ|SMBJ|SMCJ|SP0\d|PESD|TPD\d|USBLC|PRTR|ESDA|RCLAMP", re.I)


def _protection(b: Board, specs: dict) -> dict[str, list[str]]:
    """net -> parts that shunt it to ground as ESD / TVS protection."""
    out = defaultdict(list)
    by_ref = defaultdict(list)
    for p in b.pads:
        by_ref[p["ref"]].append(p)
    fpv = {f["ref"]: f for f in b.footprints}
    for ref, ps in by_ref.items():
        if not re.match(r"(D|TVS|ESD|U)\d", ref):
            continue
        cat = (specs.get(ref) or {}).get("category")
        text = f"{fpv.get(ref, {}).get('value', '')} {fpv.get(ref, {}).get('lib', '')}"
        if not (cat in ("tvs",) or TVS_RE.search(text)):
            continue
        if not any(is_ground_net(p["net_name"]) for p in ps):
            continue
        for p in ps:
            if p["net_name"] and not is_ground_net(p["net_name"]):
                out[p["net_name"]].append(ref)
    return out


def check_io_signals(b: Board, findings: list, specs: dict, edge_keepout_mm: float,
                     esd_max_mm: float, diff_skew_ps: float) -> dict:
    fpv = {f["ref"]: f for f in b.footprints}
    by_ref = defaultdict(list)
    for p in b.pads:
        by_ref[p["ref"]].append(p)
    prot = _protection(b, specs)
    series = defaultdict(list)             # net -> two-pin series parts (R / L / FB)
    for ref, ps in by_ref.items():
        if len(ps) == 2 and re.match(r"(R|L|FB|FL)\d", ref) and \
                not any(is_ground_net(p["net_name"]) for p in ps):
            for p in ps:
                series[p["net_name"]].append(ref)

    # connectors: ESD / filtering per external line
    io = []
    for ref in sorted(by_ref, key=_nat_ref):
        if not any(re.fullmatch(pre + r"\d+", ref) for pre in CONNECTOR_PREFIXES):
            continue
        if fpv.get(ref, {}).get("dnp"):
            continue
        for p in by_ref[ref]:
            net = p["net_name"]
            if not net or is_ground_net(net) or leaf(net).startswith("unconnected-"):
                continue
            tvs = prot.get(net, [])
            dist = None
            if tvs:
                dist = min(math.dist(p["point"], q["point"]) for t in tvs for q in by_ref[t]
                           if q["net_name"] == net)
            rec = {"connector": ref, "pin": p["pad"], "net": net,
                   "kind": "power" if is_power_net(net) else "signal",
                   "esd": tvs, "esd_distance_mm": round(dist, 1) if dist is not None else None,
                   "series_parts": series.get(net, [])}
            io.append(rec)
    # one finding per connector (and line kind): a 50-pin header is one
    # decision, not 50; `nets` lists the lines
    seen_net = set()
    no_esd, no_filt = defaultdict(list), defaultdict(list)
    for rec in io:
        key = (rec["connector"], rec["net"])
        if key in seen_net:
            continue
        seen_net.add(key)
        if not rec["esd"]:
            no_esd[(rec["connector"], rec["kind"])].append(rec["net"])
        elif rec["esd_distance_mm"] is not None and rec["esd_distance_mm"] > esd_max_mm:
            findings.append(_f("warning", "EM-IO-ESD-DIST",
                               f"{rec['connector']} line {rec['net']}: protection "
                               f"{', '.join(rec['esd'])} is {rec['esd_distance_mm']} mm from the "
                               f"pin (> {esd_max_mm} mm); the ESD current runs through the board "
                               "before it is clamped.", ref=rec["connector"], net=rec["net"]))
        if rec["kind"] == "signal" and not rec["series_parts"]:
            no_filt[rec["connector"]].append(rec["net"])

    def _lines(nets):
        return ", ".join(nets[:8]) + (f" and {len(nets) - 8} more" if len(nets) > 8 else "")
    for (ref, k), nets in no_esd.items():
        one = len(nets) == 1
        findings.append(_f("warning" if k == "signal" else "info", "EM-IO-ESD",
                           f"{ref} {k} line{'' if one else f's ({len(nets)})'} {_lines(nets)} "
                           f"{'has' if one else 'have'} no TVS / ESD diode to ground (by value, "
                           "library or datasheet category). Board-to-board and test headers "
                           "need none.", ref=ref, net=nets[0], nets=nets))
    for ref, nets in no_filt.items():
        one = len(nets) == 1
        findings.append(_f("info", "EM-IO-FILTER",
                           f"{ref} signal{'' if one else f's ({len(nets)})'} {_lines(nets)} "
                           f"{'leaves' if one else 'leave'} the board with no series R / ferrite "
                           "/ inductor (no common-mode or edge-rate filtering).",
                           ref=ref, net=nets[0], nets=nets))

    # tracks near the board edge
    near_edge = defaultdict(float)
    for t in b.tracks:
        if not is_signal_net(t["net_name"]):
            continue
        n = max(2, int(math.dist(t["start"], t["end"]) / 0.5) + 1)
        for k in range(n):
            x = t["start"][0] + (t["end"][0] - t["start"][0]) * k / (n - 1)
            y = t["start"][1] + (t["end"][1] - t["start"][1]) * k / (n - 1)
            if _edge_distance(b, x, y) < edge_keepout_mm:
                near_edge[t["net_name"]] += math.dist(t["start"], t["end"]) / n
    for net, length in sorted(near_edge.items()):
        hs = bool(CLOCK_RE.search(leaf(net)) or HIGH_SPEED_RE.search(leaf(net)))
        findings.append(_f("warning" if hs else "info", "EM-EDGE-TRACE",
                           f"{net}: {length:.1f} mm of track within {edge_keepout_mm} mm of the "
                           "board edge (fringing field, no plane beyond the edge).", net=net))

    # clocks: length on outer layers, near connectors / edge
    conns = [f for f in b.footprints
             if any(re.fullmatch(pre + r"\d+", f["ref"]) for pre in CONNECTOR_PREFIXES)]
    clocks = defaultdict(lambda: {"length_mm": 0.0, "outer_mm": 0.0, "min_connector_mm": math.inf})
    outer = {b.layers[0], b.layers[-1]} if b.layers else set()
    for t in b.tracks:
        if not CLOCK_RE.search(leaf(t["net_name"])):
            continue
        c = clocks[t["net_name"]]
        length = math.dist(t["start"], t["end"])
        c["length_mm"] += length
        if t["layer"] in outer:
            c["outer_mm"] += length
        for f in conns:
            c["min_connector_mm"] = min(c["min_connector_mm"],
                                        math.dist(t["start"], (f["x"], f["y"])),
                                        math.dist(t["end"], (f["x"], f["y"])))
    for net, c in clocks.items():
        if c["min_connector_mm"] < 10:
            findings.append(_f("warning", "EM-CLK-CONN",
                               f"Clock {net} passes {c['min_connector_mm']:.1f} mm from a connector: "
                               "couples onto the cable (common-mode emission).", net=net))
        c["length_mm"] = round(c["length_mm"], 1)
        c["outer_mm"] = round(c["outer_mm"], 1)
        c["min_connector_mm"] = round(c["min_connector_mm"], 1) \
            if c["min_connector_mm"] != math.inf else None

    # crystals: distance to the IC they drive
    xtals = []
    for ref, ps in by_ref.items():
        text = f"{fpv.get(ref, {}).get('value', '')} {fpv.get(ref, {}).get('lib', '')}"
        is_xtal = re.fullmatch(r"Y\d+", ref) or (
            re.fullmatch(r"X\d+", ref) and 2 <= len(ps) <= 4
            and re.search(r"crystal|xtal|MHz|kHz|resonator", text, re.I))
        if not is_xtal:
            continue
        for p in ps:
            if is_ground_net(p["net_name"]) or not p["net_name"]:
                continue
            ics = [(math.dist(p["point"], q["point"]), q["ref"]) for q in b.pads
                   if q["net_name"] == p["net_name"] and re.match(r"(U|IC)\d", q["ref"])]
            if ics:
                d, ic = min(ics)
                xtals.append({"ref": ref, "net": p["net_name"], "ic": ic, "distance_mm": round(d, 1)})
                if d > 10:
                    findings.append(_f("warning", "EM-XTAL-DIST",
                                       f"Crystal {ref} pin on {p['net_name']} is {d:.1f} mm from "
                                       f"{ic}: long oscillator traces radiate and pick up noise.",
                                       ref=ref))

    # differential pairs: skew
    eps_eff = (b.eps_r() + 1) / 2 if b.layers else 3.0
    ps_per_mm = math.sqrt(eps_eff) / C0 * 1e3 * 1e12
    lengths = defaultdict(float)
    for t in b.tracks:
        lengths[t["net_name"]] += math.dist(t["start"], t["end"])
    pairs = []
    for pr in pcb_editor.find_diff_pair_candidates(b.tree):
        lp, ln = lengths.get(pr["p"], 0.0), lengths.get(pr["n"], 0.0)
        if not lp and not ln:
            continue
        skew = abs(lp - ln) * ps_per_mm
        rec = {"pair": pr["base_name"], "p": pr["p"], "n": pr["n"], "p_mm": round(lp, 2),
               "n_mm": round(ln, 2), "skew_ps": round(skew, 1)}
        pairs.append(rec)
        if skew > diff_skew_ps:
            findings.append(_f("warning", "EM-DIFF-SKEW",
                               f"Pair {pr['base_name']}: {lp:.2f} vs {ln:.2f} mm, skew ~{skew:.0f} ps "
                               f"(> {diff_skew_ps} ps): skew converts the pair into common mode.",
                               net=pr["p"]))

    # 3H spacing for clock / high-speed nets
    h_ref = {}
    for d in b.dielectrics:
        a, c = d["between"]
        if d.get("thickness_mm"):
            h_ref[a] = min(h_ref.get(a, 9e9), d["thickness_mm"])
            h_ref[c] = min(h_ref.get(c, 9e9), d["thickness_mm"])
    xt = []
    hot = [t for t in b.tracks if CLOCK_RE.search(leaf(t["net_name"]))
           or HIGH_SPEED_RE.search(leaf(t["net_name"]))]
    for t in hot:
        h = h_ref.get(t["layer"])
        if not h:
            continue
        for o in b.tracks:
            if o["layer"] != t["layer"] or o["net_name"] == t["net_name"] or not o["net_name"] \
                    or is_ground_net(o["net_name"]):
                continue
            gap, overlap = _parallel_gap(t, o)
            if overlap >= 2.0 and gap < 3 * h:
                key = tuple(sorted((t["net_name"], o["net_name"])))
                if key not in {tuple(sorted((x["a"], x["b"]))) for x in xt}:
                    xt.append({"a": t["net_name"], "b": o["net_name"], "layer": t["layer"],
                               "gap_mm": round(gap, 3), "overlap_mm": round(overlap, 1),
                               "three_h_mm": round(3 * h, 3)})
    for x in xt:
        findings.append(_f("warning", "EM-3H",
                           f"{x['a']} runs {x['overlap_mm']} mm parallel to {x['b']} on {x['layer']} "
                           f"at {x['gap_mm']} mm edge gap (< 3H = {x['three_h_mm']} mm).",
                           net=x["a"]))
    roles = layer_roles(b)
    for rec in pairs:
        layers = sorted({t["layer"] for t in b.tracks if t["net_name"] in (rec["p"], rec["n"])},
                        key=lambda x: b.layers.index(x) if x in b.layers else 99)
        rec["layers"] = layers
        if len(layers) > 1:
            findings.append(_f("info", "EM-DIFF-LAYERS",
                               f"Pair {rec['pair']} is routed on {', '.join(layers)}: every layer "
                               "change is a differential-to-common-mode conversion point; give "
                               "each transition a ground via next to it.", net=rec["p"]))
        rec["reference"] = _pair_reference(b, roles, rec)
        ref = rec["reference"]
        if ref["gap_mm"] > 0 or len(ref["nets"]) > 1:
            findings.append(_f("warning", "EM-DIFF-REF",
                               f"Pair {rec['pair']}: reference plane under the pair "
                               + (f"changes net ({', '.join(ref['nets'])})" if len(ref["nets"]) > 1
                                  else "is continuous in net")
                               + (f", {ref['gap_mm']} mm of the pair runs over no plane copper"
                                  if ref["gap_mm"] > 0 else "")
                               + ". The pair's return current loses its image plane there.",
                               net=rec["p"]))

    return {"io_lines": io, "clocks": dict(clocks), "crystals": xtals, "diff_pairs": pairs,
            "near_edge": {k: round(v, 1) for k, v in near_edge.items()}, "parallel_3h": xt}


def _pair_reference(b: Board, roles: list[dict], rec: dict) -> dict:
    """Plane nets seen under both nets of a pair on their adjacent plane
    layer(s), and the track length with no plane copper underneath (pad /
    via ends of 0.6 mm excluded)."""
    nets: set = set()
    gap = 0.0
    step = b.grid.res
    for t in b.tracks:
        if t["net_name"] not in (rec["p"], rec["n"]):
            continue
        refs = _ref_layers(b, roles, t["layer"])
        if not refs:
            continue
        length = math.dist(t["start"], t["end"])
        n = max(2, int(length / step) + 1)
        for k in range(n):
            s = length * k / (n - 1)
            if s < 0.6 or length - s < 0.6:
                continue
            x = t["start"][0] + (t["end"][0] - t["start"][0]) * k / (n - 1)
            y = t["start"][1] + (t["end"][1] - t["start"][1]) * k / (n - 1)
            under = [b.net_at(r, x, y) for r in refs]
            got = [u for u in under if u]
            if got:
                nets.update(got[:1])
            else:
                gap += length / (n - 1)
    return {"nets": sorted(nets), "gap_mm": round(gap, 1)}


def _connector_aperture_mm(pads: list[dict]) -> float:
    """Largest extent of a connector's pads (incl. pad radius) — the board
    edge cut-out a shield or enclosure has to leave for it."""
    xs = [p["point"][0] for p in pads]
    ys = [p["point"][1] for p in pads]
    r = max((p["radius_mm"] for p in pads), default=0.0)
    return max(max(xs) - min(xs), max(ys) - min(ys)) + 2 * r if pads else 0.0


def shielding_advisory(b: Board, findings: list, result: dict) -> dict:
    """Slot resonance of each connector aperture (f = c / 2L, L from the
    connector's pad extent) against the known switching harmonics, and the
    shielding effectiveness the radiation estimates leave to an enclosure."""
    by_ref = defaultdict(list)
    for p in b.pads:
        by_ref[p["ref"]].append(p)
    sources = []
    for s in result.get("switching") or []:
        h = s.get("harmonics")
        if h:
            f = h["fsw_hz"]
            sources += [(n * f, s["regulator"], n) for n in range(1, int(1e9 / f) + 1)]
    apertures = []
    for ref in sorted(by_ref, key=_nat_ref):
        if not any(re.fullmatch(pre + r"\d+", ref) for pre in CONNECTOR_PREFIXES):
            continue
        L = _connector_aperture_mm(by_ref[ref])
        if L <= 0:
            continue
        f_slot = C0 / (2 * L / 1000)
        near = [(f, src, n) for f, src, n in sources if 0.8 * f_slot <= f <= 1.2 * f_slot]
        apertures.append({"ref": ref, "aperture_mm": round(L, 1),
                          "slot_resonance_hz": round(f_slot),
                          "coincident_sources": [f"{src} h{n} {f / 1e6:.0f} MHz"
                                                 for f, src, n in near[:5]]})
        if near:
            findings.append(_f("warning", "EM-SHIELD-SLOT",
                               f"{ref} aperture ~{L:.0f} mm resonates at {f_slot / 1e9:.2f} GHz, "
                               f"within 20 % of {', '.join(a for a in apertures[-1]['coincident_sources'])}: "
                               "the cut-out radiates efficiently there.", ref=ref))
    needed = []
    for s in result.get("switching") or []:
        w = (s.get("radiation_estimate") or {}).get("worst")
        if w and w["margin_db"] < 6:
            needed.append({"source": s["regulator"], "shielding_db": round(6 - w["margin_db"], 1)})
    if needed:
        findings.append(_f("info", "EM-SHIELD-SE",
                           "Estimated shielding effectiveness needed for 6 dB margin: "
                           + ", ".join(f"{n['source']} {n['shielding_db']} dB" for n in needed)
                           + " (from the EM-SW-RAD estimates)."))
    return {"apertures": apertures, "shielding_needed": needed}


# ---- group: thermal effects on EMC --------------------------------------------- #

CLASS2_RE = re.compile(r"X[5-8][RSTUVP]|Y5V|Z5U", re.I)
FERRITE_RE = re.compile(r"ferrite|bead|BLM\d|MPZ\d|\bFB\b", re.I)


def check_thermal_emc(b: Board, findings: list, regulators: list[dict], rail_volts: dict,
                      specs: dict) -> dict:
    """MLCC DC-bias exposure (class-II ceramic at a large share of its rated
    voltage loses capacitance and moves its SRF) and ferrites next to
    switching regulators (permeability falls as they heat). The DC-bias loss
    itself comes only from an extracted cap datasheet (`electrical.C_bias`
    spec value at a stated bias); without one the ratio is reported and the
    datasheet curve has to be read."""
    by_ref = defaultdict(list)
    for p in b.pads:
        by_ref[p["ref"]].append(p)
    fpv = {f["ref"]: f for f in b.footprints}
    bias = []
    for ref, ps in by_ref.items():
        if not re.match(r"C\d", ref) or len(ps) != 2 or fpv.get(ref, {}).get("dnp"):
            continue
        fields = fpv.get(ref, {}).get("fields", {})
        diel = fields.get("Dielectric", "") or fpv.get(ref, {}).get("value", "")
        if not CLASS2_RE.search(diel):
            continue
        from kicad_claude.adapters.connectivity_review import _rating
        rated = _rating({"fields": fields, "value": fpv[ref]["value"]}, "Voltage")
        v = [rail_volts.get(p["net_name"], 0.0 if is_ground_net(p["net_name"]) else None)
             for p in ps]
        if rated is None or None in v:
            continue
        vb = abs(v[0] - v[1])
        ratio = vb / rated
        rec = {"ref": ref, "value": fpv[ref]["value"], "dielectric": CLASS2_RE.search(diel).group(0),
               "size": fields.get("Size", ""), "bias_v": round(vb, 3), "rated_v": rated,
               "bias_ratio": round(ratio, 2)}
        from kicad_claude.adapters.datasheet_specs import c_bias_ratio
        cb = c_bias_ratio(specs.get(ref), vb, _parse_cap(fpv[ref]["value"]))
        if cb is not None:
            rec["c_at_bias_ratio"] = round(cb, 3)
            nom = _parse_cap(fpv[ref]["value"])
            if nom:
                rec["c_at_bias_f"] = float(f"{nom * cb:.3g}")
        bias.append(rec)
        if ratio >= 0.5:
            findings.append(_f("info", "EM-TH-BIAS",
                               f"{ref} {rec['value']} {rec['dielectric']} {rec['size']} at "
                               f"{vb:.3g} V = {ratio:.0%} of its {rated:g} V rating: class-II "
                               "ceramic loses capacitance under DC bias (SRF moves up, PDN "
                               "gaps) - "
                               + (f"datasheet curve: {cb:.0%} of nominal remains." if cb is not None
                                  else "read the MPN's DC-bias curve (not in the spec cache)."),
                               ref=ref))
    ferr = []
    sw_refs = {r["ref"] for r in regulators if r["topology"] == "switching"}
    pos = {f["ref"]: (f["x"], f["y"]) for f in b.footprints}
    for ref, f in fpv.items():
        if not (re.match(r"FB\d", ref) or (re.match(r"L\d", ref) and FERRITE_RE.search(
                f"{f['value']} {f['lib']}"))):
            continue
        d, src = min(((math.dist(pos[ref], pos[s]), s) for s in sw_refs if s in pos),
                     default=(math.inf, None))
        ferr.append({"ref": ref, "nearest_switcher": src,
                     "distance_mm": round(d, 1) if src else None})
        if d < 10:
            findings.append(_f("info", "EM-TH-FERRITE",
                               f"Ferrite {ref} is {d:.1f} mm from switching regulator {src}: its "
                               "permeability (and impedance) falls as it heats - check the bead's "
                               "impedance at temperature.", ref=ref))
    return {"dc_bias": sorted(bias, key=lambda r: -r["bias_ratio"]), "ferrites": ferr}


def _parallel_gap(a: dict, b_: dict) -> tuple[float, float]:
    """Edge-to-edge gap and overlap length of two near-parallel segments."""
    (ax0, ay0), (ax1, ay1) = a["start"], a["end"]
    (bx0, by0), (bx1, by1) = b_["start"], b_["end"]
    la = math.dist(a["start"], a["end"])
    lb = math.dist(b_["start"], b_["end"])
    if la < 1e-6 or lb < 1e-6:
        return math.inf, 0.0
    ux, uy = (ax1 - ax0) / la, (ay1 - ay0) / la
    vx, vy = (bx1 - bx0) / lb, (by1 - by0) / lb
    if abs(ux * vy - uy * vx) > 0.17:           # > ~10 degrees apart
        return math.inf, 0.0
    t0 = (bx0 - ax0) * ux + (by0 - ay0) * uy
    t1 = (bx1 - ax0) * ux + (by1 - ay0) * uy
    lo, hi = max(0.0, min(t0, t1)), min(la, max(t0, t1))
    overlap = hi - lo
    if overlap <= 0:
        return math.inf, 0.0
    dist = abs((bx0 - ax0) * -uy + (by0 - ay0) * ux)
    return dist - (a["width_mm"] + b_["width_mm"]) / 2, overlap


# ---- pre-compliance test plan ------------------------------------------------- #


def test_plan(result: dict, standard: str) -> dict:
    sw = result.get("switching") or []
    bands = []
    for s in sw:
        h = s.get("harmonics")
        if not h:
            continue
        f = h["fsw_hz"]
        bands.append({"source": s["regulator"], "fsw_hz": f,
                      "conducted_150k_30M": [round(n * f) for n in range(
                          max(1, math.ceil(150e3 / f)), int(30e6 / f) + 1)][:10],
                      "radiated_from": round(h["first_harmonic_from_30MHz"] * f)})
    probes = []
    for s in sw:
        probes += [{"point": n, "why": f"{s['regulator']} switch node"} for n in s["switch_nets"]]
    io = (result.get("io") or {}).get("io_lines", [])
    for c in sorted({r["connector"] for r in io}):
        probes.append({"point": c, "why": "connector / cable exit (common-mode current clamp)"})
    risks = [f for f in result.get("findings", []) if f["severity"] in ("error", "warning")]
    return {"standard": standard,
            "radiated_limits": [{"f_mhz": [lo, hi], "dbuv_m": lim, "distance_m": d}
                                for lo, hi, lim, d in LIMITS[standard]],
            "tests": ["conducted emissions 150 kHz - 30 MHz on the supply input (LISN)",
                      f"radiated emissions 30 MHz - 1 GHz ({standard})",
                      "ESD IEC 61000-4-2 on every connector pin and the enclosure"],
            "switching_harmonics": bands, "near_field_probe_points": probes,
            "top_risks": [f"{f['rule']}: {f['message'][:160]}" for f in risks[:15]]}


# ---- entry ------------------------------------------------------------------ #

GROUPS = ("ground", "switching", "decoupling", "io", "thermal")


# ---- harmonic-limit screen ------------------------------------------------------ #

HARMONIC_UNITS = ("dBuV/m", "dBuA")
MAX_HARMONICS = 5000


def validate_limit_profile(profile: dict) -> dict:
    """A caller's limit profile: {"name", "unit": "dBuV/m" (radiated, with
    "distance_m") | "dBuA" (harmonic current at the switch), "bands": [{"name",
    "f_start_hz", "f_stop_hz", "limit_db", "limit_stop_db" (optional: the
    limit slopes linearly in log f from start to stop)}]}. Raises ValueError
    naming the first problem. No limit tables are shipped.
    Ported in behaviour from kicad-happy 3.0.1 (MIT, © 2025 Andrew Klofas):
    emc/scripts/analyze_harmonic_limits.py:validate_limit_profile."""
    if not isinstance(profile, dict):
        raise ValueError("harmonic_limits: an object {name, unit, bands, distance_m}")
    name = profile.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("harmonic_limits.name: required (where the limits come from)")
    unit = profile.get("unit")
    if unit not in HARMONIC_UNITS:
        raise ValueError(f"harmonic_limits.unit: {unit!r} not in {list(HARMONIC_UNITS)}")
    dist = profile.get("distance_m")
    if unit == "dBuV/m" and (isinstance(dist, bool) or not isinstance(dist, (int, float))
                             or dist <= 0):
        raise ValueError("harmonic_limits.distance_m: required for dBuV/m, > 0")
    bands = profile.get("bands")
    if not isinstance(bands, list) or not bands:
        raise ValueError("harmonic_limits.bands: a non-empty list")
    out = []
    for i, b in enumerate(bands):
        where = f"harmonic_limits.bands[{i}]"
        if not isinstance(b, dict):
            raise ValueError(f"{where}: an object")
        try:
            lo, hi, lim = float(b["f_start_hz"]), float(b["f_stop_hz"]), float(b["limit_db"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"{where}: f_start_hz, f_stop_hz and limit_db numbers ({e})") from e
        if not 0 < lo < hi:
            raise ValueError(f"{where}: 0 < f_start_hz < f_stop_hz")
        stop = b.get("limit_stop_db")
        if stop is not None and (isinstance(stop, bool) or not isinstance(stop, (int, float))):
            raise ValueError(f"{where}.limit_stop_db: a number")
        out.append({"name": str(b.get("name") or f"band {i + 1}"), "f_start_hz": lo,
                    "f_stop_hz": hi, "limit_db": lim,
                    "limit_stop_db": float(stop) if stop is not None else lim})
    return {"name": name, "unit": unit, "distance_m": dist, "bands": out}


def _limit_at(band: dict, f: float) -> float:
    lo, hi = band["f_start_hz"], band["f_stop_hz"]
    t = math.log(f / lo) / math.log(hi / lo)
    return band["limit_db"] + (band["limit_stop_db"] - band["limit_db"]) * min(1.0, max(0.0, t))


def screen_harmonics(switching: list[dict], profile: dict, findings: list) -> dict:
    """Each switcher's trapezoid harmonic envelope (the one the radiation
    estimate uses) against the caller's profile: worst margin per band.
    dBuV/m: Ott's differential-mode E at the profile distance; dBuA: the
    harmonic current amplitude. A screen, not a measurement."""
    prof = validate_limit_profile(profile)
    out = {}
    for rec in switching or []:
        env = rec.get("envelope")
        if not env:
            continue
        f, rows = env["fsw_hz"], []
        for band in prof["bands"]:
            n0 = max(1, math.ceil(band["f_start_hz"] / f))
            n1 = min(int(band["f_stop_hz"] // f), n0 + MAX_HARMONICS)
            worst = None
            for n in range(n0, n1 + 1):
                fn = n * f
                i_n = trapezoid_harmonic(n, env["load_current_a"], env["duty"],
                                         env["rise_time_s"], f)
                if prof["unit"] == "dBuA":
                    level = 20 * math.log10(i_n * 1e6) if i_n > 0 else -999.0
                else:
                    e = K_DM_GROUND * fn ** 2 * env["loop_area_mm2"] * 1e-6 * i_n / prof["distance_m"]
                    level = 20 * math.log10(e * 1e6) if e > 0 else -999.0
                lim = _limit_at(band, fn)
                if worst is None or lim - level < worst["margin_db"]:
                    worst = {"band": band["name"], "harmonic": n, "f_hz": round(fn),
                             "level": round(level, 1), "limit": round(lim, 1),
                             "margin_db": round(lim - level, 1)}
            rows.append(worst or {"band": band["name"], "margin_db": None,
                                  "note": "no harmonic in the band"})
            if worst and worst["margin_db"] < 0:
                findings.append(_f("warning", "EM-HARM-LIMIT",
                                   f"{rec['regulator']}: harmonic {worst['harmonic']} at "
                                   f"{worst['f_hz'] / 1e6:.3g} MHz = {worst['level']} "
                                   f"{prof['unit']} over {prof['name']} {band['name']} "
                                   f"({worst['limit']}): {worst['margin_db']} dB. Screening "
                                   "estimate only.", regulator=rec["regulator"]))
        out[rec["regulator"]] = rows
    return {"profile": prof["name"], "unit": prof["unit"], "distance_m": prof["distance_m"],
            "screening_only": True, "regulators": out}


def analyze(tree: list, groups: tuple[str, ...] = GROUPS, f_max_hz: float = 200e6,
            stitch_spacing_mm: float | None = None, resolution_mm: float = 0.5,
            power: dict | None = None, nl=None, specs: dict | None = None,
            fsw: dict | None = None, iout: dict | None = None,
            standard: str = "fcc-class-b", rise_time_s: float = 10e-9,
            decap_max_mm: float = 3.0, cap_via_max_mm: float = 1.5, pdn: dict | None = None,
            edge_keepout_mm: float = 1.0, esd_max_mm: float = 10.0,
            diff_skew_ps: float = 25.0, plan: bool = True,
            harmonic_limits: dict | None = None) -> dict:
    """`power`: an `analyze_power_tree` result and `nl` its Netlist (needed by
    the switching and decoupling groups); `specs`: {ref: spec record}."""
    if standard not in LIMITS:
        raise ValueError(f"standard must be one of {sorted(LIMITS)}")
    if harmonic_limits is not None:
        validate_limit_profile(harmonic_limits)          # fail before the slow part
    b = load_board(tree, resolution_mm)
    findings: list[dict] = []
    result: dict = {"resolution_mm": resolution_mm, "board_area_mm2":
                    round(b.board_cells * resolution_mm ** 2, 1),
                    "f_max_hz": f_max_hz, "unfilled_zone_polygons": b.unfilled_zones}
    if b.unfilled_zones:
        findings.append(_f("warning", "EM-ZONES-UNFILLED",
                           f"{b.unfilled_zones} zone layer(s) have no fill in the file; copper "
                           "checks ignore them. Refill (run_drc refill_zones=True) and re-run."))
    if "ground" in groups:
        result["ground"] = check_ground_stackup(b, findings, f_max_hz, stitch_spacing_mm)
    if "switching" in groups:
        if power is None or nl is None:
            findings.append(_f("info", "EM-SW-NONET", "Switching checks need the schematic "
                               "netlist (regulators); skipped."))
        else:
            volts = {k: v["volts"] for k, v in power.get("rail_voltages", {}).items()}
            result["switching"] = check_switching(b, findings, power["regulators"], nl, volts,
                                                  fsw or {}, iout or {}, specs or {}, standard,
                                                  rise_time_s)
            if harmonic_limits is not None:
                result["harmonic_screen"] = screen_harmonics(result["switching"],
                                                             harmonic_limits, findings)
    if "decoupling" in groups:
        volts = {k: v["volts"] for k, v in (power or {}).get("rail_voltages", {}).items()}
        if nl is None:
            findings.append(_f("info", "EM-DEC-NONET", "Supply-pin decoupling needs the "
                               "schematic netlist (pin types); only cap vias and banks checked."))
        result["decoupling"] = check_decoupling(b, findings, nl, volts, specs or {},
                                                decap_max_mm, cap_via_max_mm, pdn or {})
    if "io" in groups:
        result["io"] = check_io_signals(b, findings, specs or {}, edge_keepout_mm, esd_max_mm,
                                        diff_skew_ps)
        result["io"]["shielding"] = shielding_advisory(b, findings, result)
    if "thermal" in groups:
        volts = {k: v["volts"] for k, v in (power or {}).get("rail_voltages", {}).items()}
        result["thermal"] = check_thermal_emc(b, findings, (power or {}).get("regulators", []),
                                              volts, specs or {})
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["rule"]))
    result["findings"] = findings
    result["counts"] = {k: sum(1 for f in findings if f["severity"] == k) for k in order}
    if plan:
        result["test_plan"] = test_plan(result, standard)
    return result
