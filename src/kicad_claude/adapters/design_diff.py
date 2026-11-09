"""Design diff between two revisions of a KiCAD project (OP-3).

Schematic side compares KiCAD's own kicadsexpr netlists (whole hierarchy):
parts added / removed, Value / Footprint / field / DNP changes, and every pin
whose net changed (net renames show up as one rename, not as every pin).
PCB side compares the board files: footprints added / removed / moved /
rotated / flipped, track length and segment count per net, via count, zone
count per net and layer, board outline size.

A revision is a project folder, or a git ref of the active project:
`snapshot_git()` writes every `.kicad_*` file and library table of the
project folder at that ref into a temporary folder (`git show`, read-only;
the working tree is not touched).
"""

from __future__ import annotations

import math
import subprocess
from collections import defaultdict
from pathlib import Path

from kicad_claude.adapters import pcb_netlist, power_review
from kicad_claude.adapters.pcb_editor import get_footprint_reference, iter_footprints
from kicad_claude.adapters.pcb_geometry import board_outline_loops, zone_layers
from kicad_claude.adapters.sch_io import find_child, find_children

PROJECT_FILES = (".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_dru")
TABLES = ("sym-lib-table", "fp-lib-table")


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", check=False)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def snapshot_git(project_dir: Path, ref: str, dest: Path) -> Path:
    """Write the project folder's KiCAD files at `ref` into `dest`."""
    project_dir = Path(project_dir).resolve()
    top = Path(_git(project_dir, "rev-parse", "--show-toplevel").strip())
    rel = project_dir.relative_to(top).as_posix()
    names = _git(top, "ls-tree", "-r", "--name-only", ref, "--", rel).splitlines()
    n = 0
    for name in names:
        p = Path(name)
        if p.parent.as_posix() != rel or not (p.suffix in PROJECT_FILES or p.name in TABLES):
            continue
        data = subprocess.run(["git", "-C", str(top), "show", f"{ref}:{name}"],
                              capture_output=True, check=False)
        if data.returncode == 0:
            (dest / p.name).write_bytes(data.stdout)
            n += 1
    if not n:
        raise FileNotFoundError(f"no KiCAD files under {rel} at {ref}")
    return dest


# ---- schematic -------------------------------------------------------------- #


def _pin_map(nl: power_review.Netlist) -> dict[tuple[str, str], str]:
    return {(p.ref, p.number): net for net, pins in nl.nets.items() for p in pins}


def diff_netlists(a: power_review.Netlist, b: power_review.Netlist) -> dict:
    ra, rb = set(a.components), set(b.components)
    changed = []
    for ref in sorted(ra & rb, key=_nat):
        ca, cb = a.components[ref], b.components[ref]
        d = {}
        for key in ("value", "footprint", "lib_id", "dnp"):
            if ca.get(key) != cb.get(key):
                d[key] = [ca.get(key), cb.get(key)]
        fa, fb = ca.get("fields", {}), cb.get("fields", {})
        fd = {k: [fa.get(k), fb.get(k)] for k in sorted(set(fa) | set(fb))
              if fa.get(k) != fb.get(k)}
        if fd:
            d["fields"] = fd
        if d:
            changed.append({"ref": ref, **d})
    pa, pb = _pin_map(a), _pin_map(b)
    # net renames: a net whose whole pin set moved to one new name
    renames = {}
    for net, pins in a.nets.items():
        keys = {(p.ref, p.number) for p in pins}
        targets = {pb.get(k) for k in keys if k in pb}
        if len(targets) == 1:
            t = next(iter(targets))
            if t != net and t is not None and {(p.ref, p.number) for p in b.nets.get(t, [])} == keys:
                renames[net] = t
    moved = []
    for key in sorted(set(pa) & set(pb), key=lambda k: (_nat(k[0]), k[1])):
        na, nb = pa[key], pb[key]
        if na != nb and renames.get(na) != nb:
            moved.append({"pin": f"{key[0]}.{key[1]}", "from": na, "to": nb})
    return {"added": sorted(rb - ra, key=_nat), "removed": sorted(ra - rb, key=_nat),
            "changed": changed, "net_renames": [{"from": k, "to": v} for k, v in sorted(renames.items())],
            "pin_net_changes": moved,
            "nets_added": sorted(set(b.nets) - set(a.nets) - set(renames.values())),
            "nets_removed": sorted(set(a.nets) - set(b.nets) - set(renames))}


# ---- PCB ------------------------------------------------------------------ #


def _pcb_summary(tree: list) -> dict:
    fps = {}
    for fp in iter_footprints(tree):
        at = find_child(fp, "at")
        layer = find_child(fp, "layer")
        ref = get_footprint_reference(fp) or "?"
        fps[ref] = {"x": float(at[1]) if at else 0.0, "y": float(at[2]) if at else 0.0,
                    "rot": float(at[3]) if at and len(at) > 3 else 0.0,
                    "side": str(layer[1]) if layer else "", "lib": str(fp[1]) if len(fp) > 1 else ""}
    nets = defaultdict(lambda: {"length_mm": 0.0, "segments": 0})
    for t in pcb_netlist._tracks(tree):
        n = nets[t["net_name"]]
        n["length_mm"] += math.dist(t["start"], t["end"])
        n["segments"] += 1
    vias = len(find_children(tree, "via"))
    zones = defaultdict(int)
    from kicad_claude.adapters.pcb_editor import net_of
    for z in find_children(tree, "zone"):
        for layer in zone_layers(z):
            zones[f"{net_of(z, tree)}@{layer}"] += 1
    loops = board_outline_loops(tree)
    xs = [x for lp in loops for x, _ in lp]
    ys = [y for lp in loops for _, y in lp]
    size = [round(max(xs) - min(xs), 3), round(max(ys) - min(ys), 3)] if xs else None
    return {"footprints": fps, "nets": nets, "vias": vias, "zones": dict(zones), "size": size}


def diff_pcbs(a_tree: list, b_tree: list, move_tol_mm: float = 0.01) -> dict:
    a, b = _pcb_summary(a_tree), _pcb_summary(b_tree)
    fa, fb = a["footprints"], b["footprints"]
    moved = []
    for ref in sorted(set(fa) & set(fb), key=_nat):
        x, y = fa[ref], fb[ref]
        d = {}
        dist = math.dist((x["x"], x["y"]), (y["x"], y["y"]))
        if dist > move_tol_mm:
            d["moved_mm"] = round(dist, 3)
            d["from"] = [x["x"], x["y"]]
            d["to"] = [y["x"], y["y"]]
        if abs(((x["rot"] - y["rot"]) + 180) % 360 - 180) > 0.01:
            d["rotation"] = [x["rot"], y["rot"]]
        if x["side"] != y["side"]:
            d["side"] = [x["side"], y["side"]]
        if x["lib"] != y["lib"]:
            d["footprint"] = [x["lib"], y["lib"]]
        if d:
            moved.append({"ref": ref, **d})
    nets = []
    for n in sorted(set(a["nets"]) | set(b["nets"])):
        la = a["nets"].get(n, {"length_mm": 0.0, "segments": 0})
        lb = b["nets"].get(n, {"length_mm": 0.0, "segments": 0})
        if abs(la["length_mm"] - lb["length_mm"]) > 0.01 or la["segments"] != lb["segments"]:
            nets.append({"net": n, "length_mm": [round(la["length_mm"], 2), round(lb["length_mm"], 2)],
                         "segments": [la["segments"], lb["segments"]]})
    zones = [{"zone": k, "count": [a["zones"].get(k, 0), b["zones"].get(k, 0)]}
             for k in sorted(set(a["zones"]) | set(b["zones"]))
             if a["zones"].get(k, 0) != b["zones"].get(k, 0)]
    return {"footprints_added": sorted(set(fb) - set(fa), key=_nat),
            "footprints_removed": sorted(set(fa) - set(fb), key=_nat),
            "footprints_changed": moved, "routing_changed": nets,
            "vias": [a["vias"], b["vias"]], "zones_changed": zones,
            "board_size_mm": [a["size"], b["size"]]}


def _nat(s: str) -> tuple:
    import re
    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s))
