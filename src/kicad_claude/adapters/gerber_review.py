"""Gerber (RS-274X / X2) and Excellon drill analysis of a fab output folder (OP-3).

Reads what was actually exported — not the board file — so it catches what
the fab will see: missing or stale files, a layer count that does not match
the job file, an outline that does not match the declared board size, drills
outside the outline, and minimum conductor width / drill below a fab
preset's capability.

Parsing covers what KiCAD writes: `%FSLAX..Y..%`, `%MOMM%` / `%MOIN%`,
`%TF.FileFunction`, `%TA.AperFunction` before an aperture definition,
`%ADDnn C|R|O|<macro>,...%`, D01 / D02 / D03 operations, G36 / G37 regions,
and Excellon `TnnC<diam>` tools with `; #@! TA.AperFunction` comments and
`TF.FileFunction,Plated|NonPlated`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

GERBER_EXT = {".gbr", ".gtl", ".gbl", ".gts", ".gbs", ".gto", ".gbo", ".gtp", ".gbp",
              ".gm1", ".gko", ".gml"} | {f".g{i}" for i in range(1, 31)}
DRILL_EXT = {".drl", ".xln", ".exc"}


def _f(sev: str, rule: str, msg: str, **kw) -> dict:
    return {"severity": sev, "rule": rule, "message": msg, **kw}


def parse_gerber(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    fs = re.search(r"%FSLAX(\d)(\d)Y(\d)(\d)\*%", text)
    dec = int(fs.group(2)) if fs else 6
    unit = 25.4 if "%MOIN*%" in text else 1.0
    func = re.search(r"%TF\.FileFunction,([^*]+)\*%", text)
    pol = re.search(r"%TF\.FilePolarity,([^*]+)\*%", text)
    created = re.search(r"%TF\.CreationDate,([^*]+)\*%", text)
    apertures: dict[str, dict] = {}
    pending_fn = None
    for m in re.finditer(r"%TA\.AperFunction,([^*]+)\*%|%TD\*%|%ADD(\d+)([A-Za-z_][\w.]*)"
                         r"(?:,([^*]*))?\*%", text):
        if m.group(1):
            pending_fn = m.group(1)
            continue
        if m.group(0) == "%TD*%":
            pending_fn = None
            continue
        params = [float(x) for x in (m.group(4) or "").split("X") if x.strip()]
        shape = m.group(3)
        size = params[0] * unit if params and shape in ("C", "R", "O", "P") else None
        apertures[m.group(2)] = {"shape": shape, "size_mm": size, "function": pending_fn}
    current = None
    draws = flashes = regions = 0
    min_draw = None
    xs, ys = [], []
    in_region = False
    for m in re.finditer(r"(G36|G37)|D(\d+)\*|(?:X(-?\d+))?(?:Y(-?\d+))?(?:I-?\d+)?(?:J-?\d+)?"
                         r"D0?([123])\*", text):
        if m.group(1):
            in_region = m.group(1) == "G36"
            regions += m.group(1) == "G36"
            continue
        if m.group(2) and int(m.group(2)) >= 10:
            current = m.group(2)
            continue
        op = m.group(5)
        if op is None:
            continue
        if m.group(3):
            xs.append(int(m.group(3)) / 10 ** dec * unit)
        if m.group(4):
            ys.append(int(m.group(4)) / 10 ** dec * unit)
        if op == "1" and not in_region:
            draws += 1
            ap = apertures.get(current or "", {})
            if ap.get("size_mm") and (ap.get("function") or "").startswith("Conductor"):
                min_draw = ap["size_mm"] if min_draw is None else min(min_draw, ap["size_mm"])
        elif op == "3":
            flashes += 1
    return {"file": path.name, "function": func.group(1) if func else "",
            "polarity": pol.group(1) if pol else "", "created": created.group(1) if created else "",
            "apertures": len(apertures), "draws": draws, "flashes": flashes, "regions": regions,
            "min_conductor_width_mm": round(min_draw, 4) if min_draw else None,
            "extent_mm": ([round(min(xs), 3), round(min(ys), 3), round(max(xs), 3), round(max(ys), 3)]
                          if xs and ys else None)}


def parse_drill(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    unit = 25.4 if re.search(r"^INCH", text, re.M) else 1.0
    func = re.search(r"TF\.FileFunction,([^\s]+)", text)
    tools: dict[str, dict] = {}
    pending = None
    for line in text.splitlines():
        a = re.search(r"TA\.AperFunction,([^\s]+)", line)
        if a:
            pending = a.group(1)
            continue
        t = re.match(r"^T(\d+)C([\d.]+)", line)
        if t:
            tools[t.group(1)] = {"diameter_mm": round(float(t.group(2)) * unit, 4),
                                 "function": pending, "hits": 0}
            pending = None
    current = None
    xs, ys = [], []
    slots = 0
    body = text.split("%", 1)[1] if "%" in text else text
    for line in body.splitlines():
        t = re.match(r"^T(\d+)\s*$", line)
        if t:
            current = t.group(1)
            continue
        c = re.match(r"^X(-?[\d.]+)Y(-?[\d.]+)(G85X(-?[\d.]+)Y(-?[\d.]+))?", line)
        if c and current in tools:
            tools[current]["hits"] += 1
            xs.append(float(c.group(1)) * unit)
            ys.append(float(c.group(2)) * unit)
            slots += bool(c.group(3))
    return {"file": path.name, "function": func.group(1) if func else "",
            "tools": tools, "holes": sum(t["hits"] for t in tools.values()), "slots": slots,
            "min_drill_mm": min((t["diameter_mm"] for t in tools.values() if t["hits"]), default=None),
            "extent_mm": ([round(min(xs), 3), round(min(ys), 3), round(max(xs), 3), round(max(ys), 3)]
                          if xs else None)}


def analyze(folder: Path, preset_rules: dict | None = None, board_file: Path | None = None) -> dict:
    folder = Path(folder)
    files = sorted(p for p in folder.iterdir() if p.is_file())
    findings: list[dict] = []
    gerbers = [parse_gerber(p) for p in files if p.suffix.lower() in GERBER_EXT
               and "drl_map" not in p.name]
    drills = [parse_drill(p) for p in files if p.suffix.lower() in DRILL_EXT]
    job_path = next((p for p in files if p.suffix.lower() == ".gbrjob"), None)
    job = json.loads(job_path.read_text(encoding="utf-8")) if job_path else None

    by_fn = {g["function"]: g for g in gerbers}
    copper = [g for g in gerbers if g["function"].startswith("Copper")]
    outline = next((g for g in gerbers if g["function"].startswith("Profile")), None)
    result = {"folder": str(folder), "gerbers": gerbers, "drills": drills,
              "copper_layers": len(copper)}

    if not gerbers:
        findings.append(_f("error", "GB-NONE", f"No Gerber files in {folder}."))
    if not outline:
        findings.append(_f("error", "GB-NO-OUTLINE", "No board outline (Profile / Edge.Cuts) file."))
    for need in ("Soldermask,Top", "Soldermask,Bot", "Legend,Top"):
        if not any(fn.startswith(need) for fn in by_fn):
            findings.append(_f("info", "GB-LAYER-MISSING", f"No {need} layer exported."))
    if not drills:
        findings.append(_f("warning", "GB-NO-DRILL", "No Excellon drill file."))

    if job:
        result["job"] = {"size": job.get("GeneralSpecs", {}).get("Size"),
                         "layers": job.get("GeneralSpecs", {}).get("LayerNumber"),
                         "thickness": job.get("GeneralSpecs", {}).get("BoardThickness")}
        fab_fns = ("Copper", "Soldermask", "Legend", "Profile", "Paste", "Plated", "NonPlated")
        for fa in job.get("FilesAttributes", []):
            if not (folder / fa.get("Path", "")).is_file():
                fab = (fa.get("FileFunction") or "").startswith(fab_fns)
                findings.append(_f("error" if fab else "info", "GB-MISSING-FILE",
                                   f"Job file lists {fa.get('Path')} ({fa.get('FileFunction')}) but it "
                                   "is not in the folder"
                                   + ("." if fab else " (not a fabrication layer).")))
        n = result["job"]["layers"]
        if n and n != len(copper):
            findings.append(_f("error", "GB-LAYERS", f"Job file says {n} copper layers, folder has "
                               f"{len(copper)} copper Gerbers."))
        size = result["job"]["size"]
        if size and outline and outline["extent_mm"]:
            x0, y0, x1, y1 = outline["extent_mm"]
            w, h = x1 - x0, y1 - y0
            if abs(w - size["X"]) > 0.2 or abs(h - size["Y"]) > 0.2:
                findings.append(_f("warning", "GB-SIZE", f"Outline extent {w:.2f} x {h:.2f} mm vs job "
                                   f"size {size['X']} x {size['Y']} mm."))
    else:
        findings.append(_f("info", "GB-NO-JOB", "No .gbrjob file: layer count and size not "
                           "cross-checked."))

    if outline and outline["extent_mm"]:
        x0, y0, x1, y1 = outline["extent_mm"]
        for d in drills:
            e = d["extent_mm"]
            if e and (e[0] < x0 - 0.01 or e[1] < y0 - 0.01 or e[2] > x1 + 0.01 or e[3] > y1 + 0.01):
                findings.append(_f("error", "GB-DRILL-OUTSIDE",
                                   f"{d['file']} has holes outside the board outline extent."))

    min_w = min((g["min_conductor_width_mm"] for g in copper if g["min_conductor_width_mm"]),
                default=None)
    min_drill = min((d["min_drill_mm"] for d in drills if d["min_drill_mm"]), default=None)
    result["min_conductor_width_mm"] = min_w
    result["min_drill_mm"] = min_drill
    result["holes"] = sum(d["holes"] for d in drills)
    if preset_rules:
        tw = preset_rules.get("min_track_width")
        dr = min(x for x in (preset_rules.get("min_via_drill"),
                             preset_rules.get("min_through_hole_diameter")) if x)
        if min_w is not None and tw and min_w < tw - 1e-6:
            findings.append(_f("error", "GB-MIN-TRACE", f"Narrowest conductor draw {min_w} mm < "
                               f"preset minimum {tw} mm."))
        if min_drill is not None and dr and min_drill < dr - 1e-6:
            findings.append(_f("error", "GB-MIN-DRILL", f"Smallest drill {min_drill} mm < preset "
                               f"minimum {dr} mm."))
    if board_file and Path(board_file).is_file():
        import datetime as _dt
        mt = _dt.datetime.fromtimestamp(Path(board_file).stat().st_mtime, _dt.timezone.utc)
        stale = []
        for g in gerbers:
            if g["created"]:
                try:
                    c = _dt.datetime.fromisoformat(g["created"])
                except ValueError:
                    continue
                if c < mt - _dt.timedelta(seconds=60):
                    stale.append(g["file"])
        if stale:
            findings.append(_f("warning", "GB-STALE", f"{len(stale)} Gerber(s) are older than "
                               f"{Path(board_file).name} (saved {mt.isoformat(timespec='seconds')}): "
                               "re-export before ordering.", files=stale))
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["rule"]))
    result["findings"] = findings
    result["counts"] = {k: sum(1 for f in findings if f["severity"] == k) for k in order}
    return result
