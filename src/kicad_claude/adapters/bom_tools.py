"""BOM lifecycle: revision diff, fab-format BOM + CPL, bulk symbol-field
write-back.

  - `diff_bom`: two netlists (any two revisions) compared per designator
    (value, footprint, MPN, manufacturer, distributor numbers, DNP,
    exclude-from-BOM) and per BOM line (quantity of each MPN, or value +
    footprint when there is no MPN);
  - `fab_bom` / `fab_cpl`: JLCPCB (Comment, Designator, Footprint, LCSC
    Part #; CPL Designator, Mid X, Mid Y, Layer, Rotation) and PCBWay (Item
    #, Designator, Qty, Manufacturer, Mfg Part #, Description / Value,
    Package / Footprint, Type, Your Instructions / Notes) from KiCAD's
    netlist and `kicad-cli pcb export pos`. Rotation offsets come from the
    caller (footprint regex -> degrees) or a per-symbol field; no fab table
    is assumed;
  - `write_fields`: set / create fields on many symbols across the whole
    sheet hierarchy in one pass, with a dry run first.
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from pathlib import Path

from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io
from kicad_claude.adapters.power_review import Netlist, kind
from kicad_claude.adapters.sch_io import find_child, is_call

BOM_FIELDS = ("MPN", "Manufacturer", "LCSC", "Mouser", "DigiKey", "Farnell", "Description")
NOT_PARTS = ("TP", "H", "MH", "FID", "NT", "LOGO", "G", "#")
ROT_FIELDS = ("JLCPCB Rotation Offset", "JLC Rotation Offset", "Rotation Offset",
              "JLCPCB_CORRECTION")


def _nat(s: str) -> tuple:
    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s))


def fp_name(fp: str) -> str:
    return fp.split(":", 1)[-1]


def is_part(ref: str) -> bool:
    return kind(ref) not in NOT_PARTS and not ref.startswith("#")


def row(nl: Netlist, ref: str) -> dict:
    c = nl.components[ref]
    f = c.get("fields") or {}
    return {"ref": ref, "value": c.get("value", ""), "footprint": fp_name(c.get("footprint", "")),
            **{k: f.get(k, "") for k in BOM_FIELDS},
            "dnp": bool(c.get("dnp")), "in_bom": bool(c.get("in_bom", True))}


def line_key(r: dict) -> str:
    return r["MPN"] or f"{r['value']} | {r['footprint']}"


def diff_bom(a: Netlist, b: Netlist, extra_fields: tuple = ()) -> dict:
    keys = ("value", "footprint", *BOM_FIELDS, "dnp", "in_bom")
    ra = {r: row(a, r) for r in a.components if is_part(r)}
    rb = {r: row(b, r) for r in b.components if is_part(r)}
    for side, nl in ((ra, a), (rb, b)):
        for ref in side:
            for k in extra_fields:
                side[ref][k] = (nl.components[ref].get("fields") or {}).get(k, "")
    added = [rb[r] for r in sorted(set(rb) - set(ra), key=_nat)]
    removed = [ra[r] for r in sorted(set(ra) - set(rb), key=_nat)]
    changed = []
    for r in sorted(set(ra) & set(rb), key=_nat):
        d = {k: [ra[r][k], rb[r][k]] for k in (*keys, *extra_fields) if ra[r][k] != rb[r][k]}
        if d:
            changed.append({"ref": r, **d})

    def lines(rows):
        out = defaultdict(list)
        for r in rows.values():
            if not r["dnp"] and r["in_bom"]:
                out[line_key(r)].append(r["ref"])
        return out
    la, lb = lines(ra), lines(rb)
    qty = []
    for k in sorted(set(la) | set(lb)):
        na, nb = len(la.get(k, [])), len(lb.get(k, []))
        if na != nb:
            qty.append({"line": k, "qty_old": na, "qty_new": nb,
                        "refs_added": sorted(set(lb.get(k, [])) - set(la.get(k, [])), key=_nat),
                        "refs_removed": sorted(set(la.get(k, [])) - set(lb.get(k, [])), key=_nat)})
    return {"added": added, "removed": removed, "changed": changed, "line_qty_changes": qty,
            "lines_old": len(la), "lines_new": len(lb),
            "fitted_old": sum(len(v) for v in la.values()),
            "fitted_new": sum(len(v) for v in lb.values()),
            "mpns_new": sorted(set(k for k in lb if k not in la and " | " not in k)),
            "mpns_gone": sorted(set(k for k in la if k not in lb and " | " not in k))}


# ---- fab formats ------------------------------------------------------------ #

FABS = ("jlcpcb", "pcbway")


def fitted_rows(nl: Netlist, exclude: set | None = None) -> list[dict]:
    return [row(nl, r) for r in sorted(nl.components, key=_nat)
            if is_part(r) and not nl.components[r].get("dnp")
            and nl.components[r].get("in_bom", True) and r not in (exclude or ())]


def _group(findings: list[dict]) -> list[dict]:
    """One finding per rule: refs joined, first message kept as example."""
    by = defaultdict(list)
    for f in findings:
        by[(f["severity"], f["rule"])].append(f)
    out = []
    for (sev, rule), fs in by.items():
        refs = [f["ref"] for f in fs]
        msg = fs[0]["message"] if len(fs) == 1 else (
            f"{len(fs)} parts: {', '.join(refs[:15])}{' ...' if len(refs) > 15 else ''}. "
            f"E.g. {fs[0]['message']}")
        out.append({"severity": sev, "rule": rule,
                    "ref": ",".join(refs[:30]) + (",..." if len(refs) > 30 else ""),
                    "message": msg})
    return out


def read_pos(path: Path) -> dict[str, dict]:
    """`kicad-cli pcb export pos --format csv --units mm` -> {ref: {...}}."""
    out = {}
    with open(path, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            out[r["Ref"]] = {"x": float(r["PosX"]), "y": float(r["PosY"]),
                             "rot": float(r["Rot"]), "side": r["Side"].strip().lower(),
                             "package": r.get("Package", "")}
    return out


def fab_bom(nl: Netlist, fab: str, smd_refs: set | None = None,
            exclude: set | None = None) -> tuple[list[list[str]], list[dict]]:
    """(rows incl. header, findings). `exclude`: consigned / hand-fitted refs."""
    rows = fitted_rows(nl, exclude)
    findings = []
    if fab == "jlcpcb":
        groups = defaultdict(list)
        for r in rows:
            groups[(r["value"], r["footprint"], r["LCSC"])].append(r["ref"])
            if not r["LCSC"]:
                findings.append({"severity": "warning", "rule": "FB-NO-LCSC", "ref": r["ref"],
                                 "message": f"{r['ref']} {r['value']} has no LCSC field: JLCPCB "
                                            "cannot place it (consign it or add the part)."})
        out = [["Comment", "Designator", "Footprint", "LCSC Part #"]]
        for (val, fp, lcsc), refs in sorted(groups.items(), key=lambda kv: _nat(kv[1][0])):
            out.append([val, ",".join(sorted(refs, key=_nat)), fp, lcsc])
        return out, _group(findings)
    groups = defaultdict(list)
    for r in rows:
        groups[(r["MPN"], r["Manufacturer"], r["value"], r["footprint"])].append(r)
        if not r["MPN"]:
            findings.append({"severity": "warning", "rule": "FB-NO-MPN", "ref": r["ref"],
                             "message": f"{r['ref']} {r['value']} has no MPN: turnkey sourcing "
                                        "needs one."})
        elif not r["Manufacturer"]:
            findings.append({"severity": "info", "rule": "FB-NO-MFR", "ref": r["ref"],
                             "message": f"{r['ref']} {r['MPN']} has no Manufacturer field: the "
                                        "fab has to guess the maker."})
    out = [["Item #", "Designator", "Qty", "Manufacturer", "Mfg Part #", "Description / Value",
            "Package/Footprint", "Type", "Your Instructions / Notes"]]
    for i, ((mpn, mfr, val, fp), rs) in enumerate(
            sorted(groups.items(), key=lambda kv: _nat(kv[1][0]["ref"])), 1):
        refs = sorted((r["ref"] for r in rs), key=_nat)
        typ = "SMD" if smd_refs is None or all(r in smd_refs for r in refs) else "THT"
        f = nl.components[rs[0]["ref"]].get("fields") or {}
        desc = " ".join([val] + [f[k] for k in ("Voltage", "Dielectric", "Tolerance") if f.get(k)])
        out.append([str(i), ",".join(refs), str(len(refs)), mfr, mpn, desc, fp, typ, ""])
    return out, _group(findings)


def fab_cpl(nl: Netlist, pos: dict, fab: str, offsets: dict | None = None,
            exclude: set | None = None) -> tuple[list, list]:
    """CPL rows for fitted parts; rotation = KiCAD rotation + offset (first
    matching footprint regex in `offsets`, else a rotation-offset field on
    the symbol), normalised to 0..359."""
    offsets = offsets or {}
    rx = [(re.compile(k), float(v)) for k, v in offsets.items()]
    out = [["Designator", "Mid X", "Mid Y", "Layer", "Rotation"]]
    findings, applied = [], []
    fitted = {r["ref"]: r for r in fitted_rows(nl, exclude)}
    for ref in sorted(fitted, key=_nat):
        p = pos.get(ref)
        if p is None:
            findings.append({"severity": "warning", "rule": "FB-NO-POS", "ref": ref,
                             "message": f"{ref} is in the BOM but not in the placement file "
                                        "(not on the board, or virtual)."})
            continue
        off = next((v for r, v in rx if r.search(fitted[ref]["footprint"])), None)
        src = "offsets"
        if off is None:
            f = nl.components[ref].get("fields") or {}
            fv = next((f[k] for k in ROT_FIELDS if f.get(k, "").strip()), None)
            if fv is not None:
                try:
                    off, src = float(fv), "field"
                except ValueError:
                    findings.append({"severity": "warning", "rule": "FB-ROT-FIELD", "ref": ref,
                                     "message": f"rotation offset field {fv!r} is not a number"})
        rot = (p["rot"] + (off or 0.0)) % 360
        if off:
            applied.append({"ref": ref, "offset": off, "source": src})
        out.append([ref, f"{p['x']:.4f}mm", f"{p['y']:.4f}mm",
                    "Top" if p["side"] in ("top", "front") else "Bottom", f"{rot:g}"])
    return out, _group(findings) + ([{"severity": "info", "rule": "FB-ROT", "ref": ",".join(
        a["ref"] for a in applied[:20]), "message": f"Rotation offsets applied to {len(applied)} "
        "parts; check the fab's placement preview."}] if applied else [])


def to_csv(rows: list[list[str]]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


# ---- bulk field write-back -------------------------------------------------- #


def sheet_files(root: Path) -> list[Path]:
    """Root schematic and every sheet file below it (each once)."""
    seen, order, frontier = set(), [], [root.resolve()]
    while frontier:
        p = frontier.pop(0)
        if p in seen or not p.is_file():
            continue
        seen.add(p)
        order.append(p)
        tree = sch_io.parse_file(p)
        for s in ed.list_sheets(tree):
            if s["filename"]:
                frontier.append((p.parent / s["filename"]).resolve())
    return order


def _instance_refs(s_node: list) -> set[str]:
    refs = set()
    inst = find_child(s_node, "instances")
    if inst is None:
        return refs
    stack = [inst]
    while stack:
        n = stack.pop()
        for c in n[1:] if isinstance(n, list) else []:
            if is_call(c, "reference") and len(c) > 1:
                refs.add(str(c[1]))
            elif isinstance(c, list):
                stack.append(c)
    return refs


def parse_updates_csv(text: str) -> dict[str, dict[str, str]]:
    """CSV with a Reference / Designator column (one ref or a comma list per
    row) and one column per field. Empty cells are skipped."""
    rd = csv.DictReader(io.StringIO(text))
    col = next((c for c in rd.fieldnames or [] if c.strip().lower() in
                ("reference", "ref", "designator", "designators")), None)
    if col is None:
        raise ValueError("CSV needs a Reference / Designator column")
    out: dict[str, dict] = defaultdict(dict)
    for r in rd:
        for ref in re.split(r"[,\s]+", r[col].strip()):
            if not ref:
                continue
            for k, v in r.items():
                if k != col and k and v is not None and v.strip() != "":
                    out[ref][k.strip()] = v.strip()
    return dict(out)


def write_fields(root: Path, updates: dict[str, dict[str, str]], create: bool = True,
                 dry_run: bool = True, save=None) -> dict:
    """Apply `{ref: {field: value}}` to every unit of each symbol, on
    whichever sheet holds it. `save(tree, path)` writes a sheet (the guarded
    writer); with `dry_run` nothing is written. `Reference` is refused."""
    bad = [r for r, f in updates.items() if "Reference" in f]
    if bad:
        raise ValueError(f"refusing to set Reference on {bad}: use annotate_schematic")
    changes, missing_field, shared = [], [], []
    found: set[str] = set()
    touched = []
    for path in sheet_files(root):
        tree = sch_io.parse_file(path)
        dirty = False
        for s in ed.iter_instance_symbols(tree):
            ref = ed.get_symbol_property(s, "Reference") or ""
            inst = _instance_refs(s)
            hits = [r for r in updates if r == ref or r in inst]
            if not hits:
                continue
            if len(inst) > 1:
                shared.append({"sheet": path.name, "symbol": ref, "instances": sorted(inst, key=_nat),
                               "note": "reused sheet: a field change applies to every instance"})
            for r in hits:
                found.add(r)
                for name, value in updates[r].items():
                    old = ed.get_symbol_property(s, name)
                    if old == value:
                        continue
                    if old is None and not create:
                        missing_field.append({"ref": r, "field": name})
                        continue
                    changes.append({"ref": r, "field": name, "old": old, "new": value,
                                    "sheet": path.name,
                                    "unit": str((find_child(s, "unit") or [None, 1])[1])})
                    if not dry_run:
                        if old is None:
                            ed.add_symbol_property(s, name, value)
                        else:
                            ed.set_symbol_property(s, name, value)
                        dirty = True
        if dirty and not dry_run:
            backup = save(tree, path)
            touched.append({"sheet": str(path), "backup": str(backup) if backup else None})
    seen_units = defaultdict(set)
    uniq = []
    for c in changes:     # one row per ref / field (units repeat the change)
        k = (c["ref"], c["field"])
        seen_units[k].add(c["unit"])
        if len(seen_units[k]) == 1:
            uniq.append({k2: v for k2, v in c.items() if k2 != "unit"})
    return {"dry_run": dry_run, "changes": uniq, "change_count": len(uniq),
            "not_found": sorted(set(updates) - found, key=_nat),
            "missing_field_not_created": missing_field,
            "reused_sheets": shared, "written": touched}
