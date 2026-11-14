"""Release tools (OP-3): Gerber analysis, design diff, what-if, lifecycle,
fab release gate.

Tools:
    analyze_gerbers   — check an exported Gerber / drill folder as the fab sees it
    diff_designs      — schematic + PCB diff between two revisions (git refs or folders)
    what_if           — change part values / DNP and see what the reviews say
    audit_lifecycle   — lifecycle / stock of every MPN at the distributors
    review_assembly   — assembly checks + schematic <-> PCB cross-verify
    fab_release_gate  — one FAB-READY / NOT FAB-READY verdict with the blockers
"""

from __future__ import annotations

import copy
import logging
import re
import tempfile
from pathlib import Path
from typing import Literal

from kicad_claude import state
from kicad_claude.arg_types import Source, clean
from kicad_claude.tools.common import on_board, project_files, snapshot
from kicad_claude.adapters import (
    assembly_review,
    connectivity_review,
    design_diff,
    gerber_review,
    kicad_cli,
    power_review,
    project_settings,
    sch_io,
    spice_auto,
)

logger = logging.getLogger("kicad-claude.tools.review.release")

NO_BOM_PREFIXES = ("TP", "H", "MH", "FID", "NT", "LOGO", "G")
EOL_RE = re.compile(r"obsolete|end of life|\beol\b|discontinu|last time buy|\bltb\b|"
                    r"no.longer.manufactured", re.I)
DIST_DROP_RE = re.compile(r"no.longer.stocked", re.I)
NRND_RE = re.compile(r"not recommended|\bnrnd\b|not for new design", re.I)
RESTRICT_RE = re.compile(r"restricted|limited availab|factory special", re.I)


def _fab_dir(fab_dir: str) -> Path:
    if fab_dir:
        return Path(fab_dir).expanduser().resolve()
    proj = state.get_active()
    for cand in (proj.path / "fab" / "gerbers", proj.path / "fab"):
        if cand.is_dir() and any(p.suffix.lower() in gerber_review.GERBER_EXT
                                 for p in cand.iterdir()):
            return cand
    return proj.path / "fab" / "gerbers"


def _dru_text(board) -> str | None:
    """The board's custom DRC rules (`<board>.kicad_dru`), if any."""
    p = Path(board).with_suffix(".kicad_dru")
    return p.read_text(encoding="utf-8") if p.is_file() else None


def _preset_rules(preset: str) -> dict | None:
    if not preset:
        return None
    if preset not in project_settings.FAB_PRESETS:
        raise ValueError(f"unknown preset {preset!r}; see list_fab_presets")
    return project_settings.FAB_PRESETS[preset]["rules"]


def _apply_changes(nl: power_review.Netlist, changes: dict) -> list[str]:
    applied = []
    for ref, ch in changes.items():
        if ref not in nl.components:
            raise ValueError(f"what_if: {ref!r} is not in the schematic")
        comp = nl.components[ref]
        if isinstance(ch, str):
            ch = {"value": ch}
        if "value" in ch:
            comp["value"] = str(ch["value"])
        if "dnp" in ch:
            comp["dnp"] = bool(ch["dnp"])
        for k, v in (ch.get("fields") or {}).items():
            comp.setdefault("fields", {})[k] = str(v)
        applied.append(f"{ref}: {ch}")
    return applied


def _review(nl, rail_voltages: dict) -> dict:
    from kicad_claude.tools.common import datasheet_values, specs_by_ref

    power = power_review.analyze(nl, datasheet=datasheet_values(nl),
                                 rail_voltages=rail_voltages)
    volts = {k: (v["volts"], v["source"]) for k, v in power["rail_voltages"].items()}
    conn = connectivity_review.analyze(nl, specs=specs_by_ref(nl), volts=volts)
    sub = spice_auto.analyze(nl, ("divider", "rc"),
                             volts={k: v[0] for k, v in volts.items()},
                             regulators=power["regulators"])
    return {"power": power, "connectivity": conn, "subcircuits": sub}


def find_alternatives(mpn: str, hits: dict, names: list[str], search, error_of,
                      limit: int = 5) -> list[dict]:
    """Replacement candidates for an end-of-life / NRND `mpn`: each source's
    suggested replacement, then the family stem searched at `names` (same
    manufacturer, same package token when the original's description names
    one, active). Ported in behaviour from kicad-happy 3.0.1 (MIT, © 2025
    Andrew Klofas): kicad/scripts/lifecycle_engine.py:find_alternatives."""
    from kicad_claude.adapters import datasheet_index as dsi
    from kicad_claude.adapters import thermal_pkg

    seen, out = {mpn.upper()}, []
    for name, h in hits.items():
        r = (h.get("suggested_replacement") or "").strip()
        if r and r.upper() not in seen:
            seen.add(r.upper())
            out.append({"mpn": r, "source": name, "why": "suggested replacement"})
    mfr = next((h.get("manufacturer") for h in hits.values() if h.get("manufacturer")), "")
    desc = " ".join(h.get("description") or "" for h in hits.values())
    pkg = (thermal_pkg.classify(desc) or {}).get("package")
    stem = dsi.stem(mpn)
    if len(stem) < 4 or stem.upper() == mpn.upper():
        stem = re.sub(r"[A-Z#\-/]{1,6}$", "", mpn, flags=re.I) or stem
    for name in names:
        if len(out) >= limit:
            break
        try:
            cands = search(name, stem, limit=10)
        except error_of(name):
            continue
        for c in cands:
            m = (c.get("mpn") or "").strip()
            text = f"{c.get('lifecycle') or ''}"
            if (not m or m.upper() in seen or not m.upper().startswith(stem.upper())
                    or EOL_RE.search(text) or NRND_RE.search(text)
                    or c.get("discontinued") or c.get("end_of_life")):
                continue
            if mfr and c.get("manufacturer") and mfr.split()[0].lower() not in \
                    c["manufacturer"].lower():
                continue
            if pkg and pkg.upper() not in (c.get("description") or "").upper():
                continue
            seen.add(m.upper())
            out.append({"mpn": m, "source": name, "manufacturer": c.get("manufacturer", ""),
                        "stock": c.get("stock"), "lifecycle": c.get("lifecycle", ""),
                        "why": f"family {stem}" + (f", package {pkg}" if pkg else "")})
            if len(out) >= limit:
                break
    return out


def register(mcp) -> None:
    @mcp.tool()
    @on_board
    def analyze_gerbers(fab_dir: str = "", preset: str = "") -> dict:
        """Check an exported Gerber / Excellon folder (default `fab/gerbers`, else
        `fab/`) the way the fab reads it: files missing vs the `.gbrjob`, layer
        count, outline size, missing outline or drill, drills outside the
        outline, Gerbers older than the board; with `preset`, narrowest trace
        and smallest drill vs the fab minimum (`GB-*` rule IDs). Per file:
        function, polarity, apertures, extent.

        Details:

        Check an exported Gerber / Excellon folder (default: the active
        project's `fab/gerbers`, else `fab/`) the way the fab will read it:
        files listed in the `.gbrjob` but missing (GB-MISSING-FILE; error for
        fabrication layers), copper layer count vs the job (GB-LAYERS), outline
        extent vs the job's board size (GB-SIZE), no outline / drill
        (GB-NO-OUTLINE, GB-NO-DRILL), drills outside the outline
        (GB-DRILL-OUTSIDE), Gerbers older than the `.kicad_pcb`
        (GB-STALE), and with `preset` (see `list_fab_presets`) the narrowest
        conductor draw and smallest drill vs the fab minimum (GB-MIN-TRACE,
        GB-MIN-DRILL). Per file: function, polarity, apertures, draws,
        flashes, regions, extent; per drill file: tool table with hits.
        """
        folder = _fab_dir(fab_dir)
        if not folder.is_dir():
            raise FileNotFoundError(f"{folder}: export Gerbers first (export kind='gerbers')")
        board = None
        proj = state.get_active_or_none()
        if proj is not None:
            board = state.get_active_board_path()
        return gerber_review.analyze(folder, _preset_rules(preset), board)

    @mcp.tool()
    def diff_designs(old: str = "git:HEAD", new: str = "", include_fields: bool = True,
                     max_items: int = 200) -> dict:
        """Diff two revisions of the active project: schematic (parts, fields,
        nets, pins moved between nets) and PCB (footprints added / moved /
        rotated / changed, routing per net, vias, zones, board size). `old` /
        `new`: `git:<ref>`, a project folder, or empty for the working tree
        (default: last commit vs working tree). Read-only.

        Details:

        `old` / `new`: `git:<ref>` (e.g. `git:HEAD~1`, `git:v1.0`) of the
        active project's folder, a project folder path, or empty for the
        working tree. Default: last commit vs working tree.

        Schematic (KiCAD's netlists of both): parts added / removed, Value /
        Footprint / lib / DNP / field changes, net renames, pins that moved to
        another net, nets added / removed. PCB: footprints added / removed /
        moved / rotated / flipped / re-footprinted, routing changes per net
        (length, segments), via count, zones per net and layer, board size.
        Read-only: git refs are written to a temporary folder.
        """
        proj = state.get_active()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out: dict = {}
            sides = []
            for tag, spec in (("old", old), ("new", new)):
                folder = snapshot(spec, tmp)
                sch, pcb = project_files(folder, proj.name)
                net = tmp / f"{tag}.net"
                kicad_cli.export_netlist(sch, net, fmt="kicadsexpr")
                sides.append((power_review.parse_netlist(net),
                              sch_io.parse_file(pcb) if pcb.is_file() else None))
                out[tag] = spec or "working tree"
        sch_diff = design_diff.diff_netlists(sides[0][0], sides[1][0])
        if not include_fields:
            for c in sch_diff["changed"]:
                c.pop("fields", None)
            sch_diff["changed"] = [c for c in sch_diff["changed"] if len(c) > 1]
        out["schematic"] = sch_diff
        if sides[0][1] is not None and sides[1][1] is not None:
            out["pcb"] = design_diff.diff_pcbs(sides[0][1], sides[1][1])
        for part in ("schematic", "pcb"):
            for k, v in (out.get(part) or {}).items():
                if isinstance(v, list) and len(v) > max_items:
                    out[part][k] = v[:max_items]
                    out[part][f"{k}_omitted"] = len(v) - max_items
        out["summary"] = {
            "parts_added": len(sch_diff["added"]), "parts_removed": len(sch_diff["removed"]),
            "parts_changed": len(sch_diff["changed"]),
            "pin_net_changes": len(sch_diff["pin_net_changes"]),
            "footprints_changed": len((out.get("pcb") or {}).get("footprints_changed", [])),
            "nets_rerouted": len((out.get("pcb") or {}).get("routing_changed", []))}
        return out

    @mcp.tool()

    @on_board
    def what_if(changes: dict, rail_voltages: dict[str, float] | None = None,
                filters: bool = True, fsw: dict[str, float] | None = None,
                loads: dict[str, float] | None = None, emc: bool = False,
                iout: dict[str, float] | None = None, netlist_path: str = "") -> dict:
        """Re-run the design reviews with hypothetical part changes, writing
        nothing: `changes={"R7": "120k", "C42": {"dnp": true}, "U4": {"fields":
        {"MPN": "..."}}}`. Reports baseline vs changed regulator Vout and
        enable, rails, dividers, RC corners and the power-tree / connectivity
        findings that appear or disappear; `filters` re-simulates LC ladders
        (ngspice), `emc` re-runs the netlist-dependent `analyze_emc` groups.

        Details:

        Compares baseline vs changed: every regulator's Vout and enable state,
        rail voltages, divider outputs (with worst case), RC corners, and the
        findings of `analyze_power_tree` + `review_connectivity` that appear
        or disappear. With `filters` (and ngspice on PATH) the LC / pi ladders
        are re-simulated too: peaking and attenuation at the 1st-3rd harmonic
        of each `fsw`, with optional `loads` (as `simulate_subcircuits`).
        With `emc`, `analyze_emc` groups that depend on the netlist
        (switching, thermal) run on the unchanged board copper for both
        netlists: findings that appear / disappear and each switcher's
        radiation-estimate margin (pass `fsw` / `iout` as for `analyze_emc`). The
        netlist is KiCAD's (exported once); datasheet specs follow the `MPN`
        field, so an MPN change picks up that part's specs.
        """
        from kicad_claude.tools.common import project_netlist

        path = project_netlist(netlist_path)
        base_nl = power_review.parse_netlist(path)
        new_nl = copy.deepcopy(base_nl)
        applied = _apply_changes(new_nl, changes)
        rv = rail_voltages or {}
        a, b = _review(base_nl, rv), _review(new_nl, rv)

        def regs(r):
            return {x["name"]: {"vout": (x["vout"] or {}).get("value"),
                                "enable": (x["enable"] or {}).get("state"),
                                "output_net": x["output_net"]} for x in r["power"]["regulators"]}

        def fkeys(r):
            fs = r["power"]["findings"] + r["connectivity"]["findings"]
            return {(f["rule"], f["ref"], f["message"]) for f in fs}

        ra, rb = regs(a), regs(b)
        reg_changes = [{"regulator": n, "before": ra.get(n), "after": rb.get(n)}
                       for n in sorted(set(ra) | set(rb)) if ra.get(n) != rb.get(n)]
        va = a["power"]["rail_voltages"]
        vb = b["power"]["rail_voltages"]
        rail_changes = [{"net": n, "before": (va.get(n) or {}).get("volts"),
                         "after": (vb.get(n) or {}).get("volts")}
                        for n in sorted(set(va) | set(vb))
                        if (va.get(n) or {}).get("volts") != (vb.get(n) or {}).get("volts")]
        da = {d["mid"]: d for d in a["subcircuits"].get("dividers", [])}
        db = {d["mid"]: d for d in b["subcircuits"].get("dividers", [])}
        div_changes = [{"node": m, "before": {k: da[m].get(k) for k in ("ratio", "v_mid", "v_mid_worst")}
                        if m in da else None,
                        "after": {k: db[m].get(k) for k in ("ratio", "v_mid", "v_mid_worst")}
                        if m in db else None}
                       for m in sorted(set(da) | set(db))
                       if (da.get(m) or {}).get("ratio") != (db.get(m) or {}).get("ratio")]
        ca = {x["node"]: x["fc_hz"] for x in a["subcircuits"].get("rc_filters", [])}
        cb = {x["node"]: x["fc_hz"] for x in b["subcircuits"].get("rc_filters", [])}
        rc_changes = [{"node": n, "fc_hz": [ca.get(n), cb.get(n)]}
                      for n in sorted(set(ca) | set(cb)) if ca.get(n) != cb.get(n)]
        ha = {x["node"]: x["fc_hz"] for x in a["subcircuits"].get("rc_highpass", [])}
        hb = {x["node"]: x["fc_hz"] for x in b["subcircuits"].get("rc_highpass", [])}
        rc_changes += [{"node": n, "kind": "high-pass", "fc_hz": [ha.get(n), hb.get(n)]}
                       for n in sorted(set(ha) | set(hb)) if ha.get(n) != hb.get(n)]
        fa, fb = fkeys(a), fkeys(b)
        fmt = lambda s: [{"rule": r, "ref": ref, "message": m} for r, ref, m in sorted(s)]  # noqa: E731
        out = {"applied": applied, "regulators": reg_changes, "rails": rail_changes,
               "dividers": div_changes, "rc_filters": rc_changes,
               "findings_new": fmt(fb - fa), "findings_gone": fmt(fa - fb)}
        if filters:
            from kicad_claude.adapters import spice_backends
            sim = spice_backends.detect("auto")
            if not sim:
                out["filters"] = f"skipped: {spice_auto.NOSIM}"
            else:
                freqs = sorted({k * f for f in (fsw or {}).values() for k in (1, 2, 3)})

                def ladders(nl, r):
                    res = spice_auto.analyze(
                        nl, ("lc",), volts={k: v["volts"] for k, v in
                                            r["power"]["rail_voltages"].items()},
                        regulators=r["power"]["regulators"],
                        sources=connectivity_review.sourced_nets(nl), freqs=freqs,
                        loads=loads or {}, sim=sim)
                    return {f"{x['input']} -> {x['output']}": x.get("response") or {}
                            for x in res.get("ladders", [])}
                la, lb = ladders(base_nl, a), ladders(new_nl, b)
                out["filters"] = [{"ladder": k,
                                   "peaking_db": [(la.get(k) or {}).get("max_peaking_db"),
                                                  (lb.get(k) or {}).get("max_peaking_db")],
                                   "attenuation_db": {f: [(la.get(k) or {}).get("at", {}).get(f),
                                                          (lb.get(k) or {}).get("at", {}).get(f)]
                                                      for f in sorted(set((la.get(k) or {}).get("at", {}))
                                                                      | set((lb.get(k) or {}).get("at", {})))}}
                                  for k in sorted(set(la) | set(lb)) if la.get(k) != lb.get(k)]
        if emc:
            from kicad_claude.adapters import emc_review
            from kicad_claude.tools.common import specs_by_ref
            board = sch_io.parse_file(state.get_active_board_path())

            def run(nl, r):
                return emc_review.analyze(board, ("switching", "thermal"), power=r["power"], nl=nl,
                                          specs=specs_by_ref(nl), fsw=fsw or {}, iout=iout or {},
                                          plan=False)
            ea, eb = run(base_nl, a), run(new_nl, b)

            def margins(e):
                return {s["regulator"]: ((s.get("radiation_estimate") or {}).get("worst") or {})
                        .get("margin_db") for s in e.get("switching", [])}
            ka = {(f["rule"], f["message"]) for f in ea["findings"]}
            kb = {(f["rule"], f["message"]) for f in eb["findings"]}
            ma, mb = margins(ea), margins(eb)
            out["emc"] = {"radiation_margin_db": {k: [ma.get(k), mb.get(k)]
                                                  for k in sorted(set(ma) | set(mb))
                                                  if ma.get(k) != mb.get(k)},
                          "findings_new": [{"rule": r, "message": m} for r, m in sorted(kb - ka)],
                          "findings_gone": [{"rule": r, "message": m} for r, m in sorted(ka - kb)],
                          "note": "board copper unchanged; only netlist-driven EMC checks re-run"}
        return out

    @mcp.tool()
    def audit_lifecycle(sources: list[Source] | None = None, mpn_field: str = "MPN",
                        max_parts: int = 200, temp_range_c: list[float] | None = None,
                        netlist_path: str = "") -> dict:
        """Lifecycle and stock of every MPN in the active schematic at each source
        in `sources` (exact MPN match, needs their API keys). Findings: LC-EOL
        (error), LC-NRND, LC-RESTRICTED, LC-STOCK (no stock anywhere),
        LC-SINGLE, LC-DIST-DROP, LC-NOTFOUND; with `temp_range_c=[-40, 85]`
        LC-TEMP (part range does not cover it) / LC-TEMP-NODATA.

        Details:

        Temperature grade: each part's operating range from its extracted
        datasheet (`recommended.T_A`, else `T_J`) or, failing that, the
        distributor's operating-temperature field, classified commercial
        (0..70) / industrial (-40..85) / extended (-40..105) / automotive
        (-40..125) / military (-55..125) / non-standard. `temp_range_c`
        given: LC-TEMP (warning) when a part's range does not cover it,
        LC-TEMP-NODATA (info, one finding) for parts with no range.

        Alternatives (LC-EOL / LC-NRND parts, `alternatives`): each source's
        suggested replacement, then a search at the queried `sources` for the
        MPN's family stem - same manufacturer, same package token in the
        description when the original names one, not end of life / NRND -
        with source, stock and lifecycle. Key electrical parameters are not
        compared: check fit before replacing; nothing is replaced.

        Per MPN and source (exact MPN match only): lifecycle text (DigiKey
        `ProductStatus`, plus its Discontinued / EndOfLife flags; Mouser
        `LifecycleStatus` and `SuggestedReplacement`; element14 / Farnell
        `productStatus`: NO_LONGER_MANUFACTURED is end of life,
        NO_LONGER_STOCKED only a distributor drop — LC-DIST-DROP, info),
        stock (`jlcpcb` adds JLC / LCSC stock, no lifecycle). Findings:
        LC-EOL (error: obsolete / end of life / discontinued / last time
        buy), LC-NRND (warning: not recommended for new designs),
        LC-RESTRICTED (warning: restricted availability), LC-STOCK (warning:
        no stock at any queried source), LC-SINGLE (info: in stock at one
        source only), LC-NOTFOUND (info). Needs the sources' API keys.
        """
        from kicad_claude.tools.common import project_netlist
        from kicad_claude.tools.sourcing.distributors import search_source, source_error

        from kicad_claude.adapters import datasheet_index as dsi
        from kicad_claude.adapters import datasheet_specs as dss
        from kicad_claude.adapters import temp_grade

        names = clean(sources, ("digikey", "mouser", "farnell"))
        if temp_range_c is not None and (len(temp_range_c) != 2
                                         or temp_range_c[0] >= temp_range_c[1]):
            raise ValueError("temp_range_c: [min C, max C], min < max")
        try:
            ds_dir = dsi.datasheets_dir(state.get_active().path)
        except Exception:  # noqa: BLE001 - no active project: distributor data only
            ds_dir = None
        nl = power_review.parse_netlist(project_netlist(netlist_path))
        mpns: dict[str, list[str]] = {}
        for ref, comp in nl.components.items():
            m = (comp["fields"].get(mpn_field) or "").strip()
            if m:
                mpns.setdefault(m, []).append(ref)
        findings, parts, errors, no_temp = [], [], {}, []
        for mpn in sorted(mpns)[:max_parts]:
            rec = {"mpn": mpn, "refs": sorted(mpns[mpn], key=design_diff._nat), "sources": {}}
            for name in names:
                try:
                    hits = search_source(name, mpn, limit=5)
                except source_error(name) as e:
                    errors[name] = str(e)[:200]
                    continue
                exact = [h for h in hits if (h.get("mpn") or "").upper() == mpn.upper()]
                if exact:
                    h = exact[0]
                    rec["sources"][name] = {k: h.get(k) for k in (
                        "lifecycle", "discontinued", "end_of_life", "suggested_replacement",
                        "stock", "unit_price", "currency", "operating_temp", "manufacturer",
                        "description") if k in h}
            parts.append(rec)
            refs = ",".join(rec["refs"][:6])
            spec = dss.load(ds_dir, mpn) if ds_dir else None
            rng = temp_grade.from_specs(spec) or temp_grade.from_sources(rec["sources"])
            if rng:
                rec["temperature"] = {"min_c": rng[0], "max_c": rng[1], "source": rng[2],
                                      "grade": temp_grade.classify(rng[0], rng[1])}
            if temp_range_c is not None:
                lo, hi = float(temp_range_c[0]), float(temp_range_c[1])
                if not rng:
                    no_temp.append((mpn, refs))
                elif rng[0] > lo or rng[1] < hi:
                    findings.append({"severity": "warning", "rule": "LC-TEMP", "ref": refs,
                                     "message": f"{mpn}: rated {rng[0]:g}..{rng[1]:g} C "
                                     f"({rec['temperature']['grade']}, {rng[2]}), does not "
                                     f"cover the required {lo:g}..{hi:g} C."})
            texts = " | ".join(f"{s}: {v.get('lifecycle')}" for s, v in rec["sources"].items()
                               if v.get("lifecycle"))
            flags = any(v.get("discontinued") or v.get("end_of_life") for v in rec["sources"].values())
            if not rec["sources"]:
                findings.append({"severity": "info", "rule": "LC-NOTFOUND", "ref": refs,
                                 "message": f"{mpn}: no exact match at {', '.join(names)}."})
                continue
            if flags or EOL_RE.search(texts) or NRND_RE.search(texts):
                rec["alternatives"] = find_alternatives(mpn, rec["sources"], names,
                                                        search_source, source_error)
                alt = (f" Alternatives: {', '.join(a['mpn'] for a in rec['alternatives'])}"
                       " (check fit before replacing)." if rec["alternatives"] else "")
            if flags or EOL_RE.search(texts):
                findings.append({"severity": "error", "rule": "LC-EOL", "ref": refs,
                                 "message": f"{mpn}: {texts or 'discontinued / end of life'}."
                                 + alt})
            elif NRND_RE.search(texts):
                findings.append({"severity": "warning", "rule": "LC-NRND", "ref": refs,
                                 "message": f"{mpn}: {texts}." + alt})
            elif RESTRICT_RE.search(texts):
                repl = [v.get("suggested_replacement") for v in rec["sources"].values()
                        if v.get("suggested_replacement")]
                findings.append({"severity": "warning", "rule": "LC-RESTRICTED", "ref": refs,
                                 "message": f"{mpn}: {texts}"
                                 + (f"; suggested replacement {repl[0]}" if repl else "") + "."})
            if DIST_DROP_RE.search(texts):
                findings.append({"severity": "info", "rule": "LC-DIST-DROP", "ref": refs,
                                 "message": f"{mpn}: {texts} (a distributor stopped stocking it; "
                                 "not a manufacturer end of life)."})
            stocked = [s for s, v in rec["sources"].items() if (v.get("stock") or 0) > 0]
            if not stocked:
                findings.append({"severity": "warning", "rule": "LC-STOCK", "ref": refs,
                                 "message": f"{mpn}: no stock at {', '.join(rec['sources'])}."})
            elif len(stocked) == 1 and len(names) > 1:
                findings.append({"severity": "info", "rule": "LC-SINGLE", "ref": refs,
                                 "message": f"{mpn}: in stock only at {stocked[0]}."})
        if no_temp:
            findings.append({"severity": "info", "rule": "LC-TEMP-NODATA",
                             "ref": ",".join(r for _, r in no_temp[:8]),
                             "message": f"No operating-temperature range for {len(no_temp)} "
                             f"MPN(s): {', '.join(m for m, _ in no_temp[:8])}"
                             f"{' ...' if len(no_temp) > 8 else ''} - extract "
                             "recommended.T_A from their datasheets."})
        order = {"error": 0, "warning": 1, "info": 2}
        findings.sort(key=lambda f: (order[f["severity"]], f["rule"], f["ref"]))
        return {"parts": parts, "findings": findings, "lookup_errors": errors,
                "counts": {k: sum(1 for f in findings if f["severity"] == k) for k in order},
                "mpn_count": len(mpns), "checked": min(len(mpns), max_parts)}

    @mcp.tool()

    @on_board
    def review_assembly(assembly_sides: Literal["auto", "top", "bottom", "both"] = "auto", min_spacing_mm: float = 0.0,
                        edge_clearance_mm: float = 0.3, fiducials_min: int = 2,
                        tall_mm: float = 5.0, tall_edge_mm: float = 3.0,
                        max_height_top_mm: float | None = None,
                        max_height_bottom_mm: float | None = None,
                        heights: bool = True,
                        cross_verify: bool = True, dfm_preset: str = "",
                        netlist_path: str = "") -> dict:
        """Assembly review of the active board plus schematic <-> PCB
        cross-verify: courtyard overlap, spacing and edge distance
        (`min_spacing_mm`, `edge_clearance_mm`), missing courtyards, SMD on an
        unpopulated side (`assembly_sides`), THT, fiducials, polarity and pin-1
        marks; `heights` from 3D models (`tall_mm`, `max_height_*_mm`); netlist
        vs board. `dfm_preset` (a `list_fab_presets` name): DFM against
        that fab + `dfm_score`. Rule IDs AS-*, XV-*, DFM-*. DNP skipped.

        Details:

        DFM (`dfm_preset`): DRC checks the project's own rules; this checks
        the copper against what the preset's fab can build. DFM-TRACK
        (track < min_track_width), DFM-SPACING (two nets' tracks / pads /
        vias on one layer < min_clearance), DFM-DRILL (via < min_via_drill,
        plated hole < min_through_hole_diameter), DFM-VIA (< min_via_diameter),
        DFM-RING (via ring < (min_via_diameter - min_via_drill) / 2), DFM-EDGE
        (copper < min_copper_edge_clearance from Edge.Cuts) - errors;
        DFM-VIA-IN-PAD (via in an SMD pad that is not an exposed pad),
        DFM-SILK (silkscreen over a pad), DFM-MASK (mask web between a
        footprint's pads below the preset's min_solder_mask_bridge or the
        board's solder_mask_min_width; else listed in `not_checked`) -
        warnings. `dfm_score` = 100 - 10 per error - 3 per warning (at most
        30 / 15 per rule), with the deductions. Zones are left to DRC.

        `complexity` (always): placements, unique parts, sides, THT,
        fine-pitch (< 0.5 mm from the footprint name) and BGA / exposed-pad
        parts, smallest passive, DNP count, and a 0-100 score with its
        factors (package mix hard 80 / medium 40 / easy 10 per placement,
        unique parts > 15, both sides, THT with SMD, fine pitch / BGA).

        Assembly: real courtyard outlines (non-convex ones too) that overlap
        (AS-CRTYD-OVERLAP; touching is fine) or sit closer than
        `min_spacing_mm` (AS-CRTYD-SPACING), courtyards beyond or within
        `edge_clearance_mm` of the edge (AS-EDGE; info for connectors),
        footprints with no courtyard (AS-NO-CRTYD), SMD parts on a side the
        assembler will not populate (`assembly_sides` top|bottom|both;
        AS-SIDE, error; default auto: the board's own sides, one AS-SIDE
        info when SMD parts sit on both), through-hole parts (AS-THT), fiducials per populated
        side (AS-FIDUCIAL), test-point coverage and supply rails without a
        test point (AS-TESTPOINT, info; `testpoints`), 0201 / 0402 passives
        with one pad joined solid to a pour (or on a via in pad landing in
        its net's pour) and the other on a track (AS-TOMBSTONE, warning) or
        a relief (info), or track widths > 3x apart (info); reliefs on both
        pads are not reported; a .kicad_dru zone_connection rule is stated;
        per-side passive orientation as data (`orientation`),
        polarized parts with no silkscreen of their own
        (AS-SILK-POLARITY) or no mark nearer pad 1 than the other pads
        (AS-PIN1-MARK: pin-1 dot, cathode bar, chamfer). DNP parts are skipped.

        Heights (`heights`): from each footprint's 3D STEP model (point
        bounding box, with the model's offset / scale / x-y rotation; a
        `.wrl` uses the `.step` next to it): parts at least `tall_mm` tall
        within `tall_edge_mm` of the edge (AS-TALL-EDGE), parts above
        `max_height_top_mm` / `max_height_bottom_mm` (AS-HEIGHT, enclosure),
        model paths that do not resolve (AS-3D-MISSING), no model
        (AS-3D-NONE). Heights are the model's, not the fitted part's
        datasheet.

        Cross-verify (KiCAD netlist vs board): XV-MISSING / XV-EXTRA, symbol
        pins with no pad of that number (XV-PADS, error), Value / Footprint /
        MPN differing between schematic and board (XV-FIELD).
        """
        if assembly_sides not in ("auto", "top", "bottom", "both"):
            raise ValueError("assembly_sides must be auto, top, bottom or both")
        board = state.get_active_board_path()
        tree = sch_io.parse_file(board)
        out = assembly_review.check_assembly(tree, assembly_sides, min_spacing_mm,
                                             edge_clearance_mm, fiducials_min, tall_mm,
                                             tall_edge_mm, max_height_top_mm,
                                             max_height_bottom_mm, heights, _dru_text(board))
        out["complexity"] = assembly_review.complexity(tree)
        if dfm_preset:
            from kicad_claude.adapters import dfm

            res = dfm.analyze(tree, dfm_preset, _preset_rules(dfm_preset))
            out["dfm"] = {k: v for k, v in res.items() if k != "findings"}
            out["findings"] = out["findings"] + res["findings"]
            order = {"error": 0, "warning": 1, "info": 2}
            out["findings"].sort(key=lambda f: (order[f["severity"]], f["rule"], f["ref"]))
            out["counts"] = {k: sum(1 for x in out["findings"] if x["severity"] == k)
                             for k in order}
        if cross_verify:
            from kicad_claude.tools.common import project_netlist
            nl = power_review.parse_netlist(project_netlist(netlist_path))
            out["cross_verify"] = assembly_review.cross_verify(tree, nl)
        return out

    @mcp.tool()

    @on_board
    def fab_release_gate(preset: str = "", fab_dir: str = "",
                         rail_voltages: dict[str, float] | None = None,
                         require_mpn: bool = False, check_lifecycle: bool = False,
                         assembly_sides: Literal["auto", "top", "bottom", "both"] = "auto",
                         loads: dict[str, float] | None = None,
                         netlist_path: str = "") -> dict:
        """One release verdict for the active project, FAB-READY or NOT FAB-READY,
        with blockers and warnings. Gates: ERC, DRC (no refill), power-tree and
        connectivity errors, Gerber folder and DFM vs `preset`, BOM MPN
        coverage (`require_mpn`), assembly + cross-verify, board thermal (with
        `loads`), optional lifecycle audit. Warnings never block. Read-only.

        Details:

        Gates (each reported with its numbers): ERC errors; DRC errors,
        unconnected items and schematic parity (run without refilling or
        saving the board); `analyze_power_tree` and `review_connectivity`
        errors; Gerber folder (`analyze_gerbers` errors, stale Gerbers, and
        the `preset` minimums); MPN coverage of the BOM (blocker only with
        `require_mpn`; test points, holes and DNP parts are exempt);
        `review_assembly` + cross-verify (their errors block: SMD on an
        unpopulated side, symbol pins without pads, parts missing from the
        board); with `preset` the DFM check of `review_assembly(dfm_preset=)`
        (DFM-* errors block, `dfm_score` reported); with `loads` the board
        thermal review of `analyze_board_thermal` (TH-TJ errors block - a Tj
        above Tj max on a datasheet Rθ); optionally `audit_lifecycle` (LC-EOL
        blocks). Warnings never block.
        Read-only.
        """
        from kicad_claude.tools.common import project_netlist

        proj = state.get_active()
        blockers, warnings, gates = [], [], {}

        erc = kicad_cli.run_erc(proj.sch_path, output_json=Path(tempfile.gettempdir())
                                / f"{proj.name}-gate.erc.json")
        gates["erc"] = {"errors": erc.get("errors", 0), "warnings": erc.get("warnings", 0)}
        if gates["erc"]["errors"]:
            blockers.append(f"ERC: {gates['erc']['errors']} error(s)")
        if gates["erc"]["warnings"]:
            warnings.append(f"ERC: {gates['erc']['warnings']} warning(s)")

        drc = kicad_cli.run_drc(state.get_active_board_path(), schematic_parity=True,
                                output_json=Path(tempfile.gettempdir()) / f"{proj.name}-gate.drc.json")
        gates["drc"] = {k: drc.get(k, 0) for k in ("errors", "warnings", "unconnected_items_count",
                                                   "schematic_parity_count")}
        for k, label in (("errors", "DRC errors"), ("unconnected_items_count", "unconnected items"),
                         ("schematic_parity_count", "schematic parity issues")):
            if gates["drc"][k]:
                blockers.append(f"DRC: {gates['drc'][k]} {label}")
        if gates["drc"]["warnings"]:
            warnings.append(f"DRC: {gates['drc']['warnings']} warning(s)")

        nl = power_review.parse_netlist(project_netlist(netlist_path))
        rev = _review(nl, rail_voltages or {})
        for name in ("power", "connectivity"):
            errs = [f for f in rev[name]["findings"] if f["severity"] == "error"]
            warns = [f for f in rev[name]["findings"] if f["severity"] == "warning"]
            gates[name] = {"errors": len(errs), "warnings": len(warns)}
            blockers += [f"{f['rule']} {f['ref']}: {f['message'][:140]}" for f in errs]
            warnings += [f"{f['rule']} {f['ref']}: {f['message'][:140]}" for f in warns]

        no_mpn = sorted((r for r, c in nl.components.items()
                         if not c["fields"].get("MPN") and not c.get("dnp")
                         and not r.startswith(NO_BOM_PREFIXES)), key=design_diff._nat)
        gates["bom"] = {"parts": len(nl.components), "without_mpn": no_mpn}
        if no_mpn:
            (blockers if require_mpn else warnings).append(
                f"BOM: {len(no_mpn)} fitted part(s) without MPN: {', '.join(no_mpn[:20])}")

        board = state.get_active_board_path()
        tree = sch_io.parse_file(board)
        asm = assembly_review.check_assembly(tree, assembly_sides, dru_text=_dru_text(board))
        if assembly_sides == "auto":
            # the gate cannot know the assembler: a second SMD side must be confirmed
            both = [f for f in asm["findings"] if f["rule"] == "AS-SIDE"]
            if both:
                warnings.append(f"AS-SIDE: {both[0]['message'][:140]} Confirm the assembler "
                                "populates both sides (assembly_sides=\"both\"), else they "
                                "are blockers.")
        xv = assembly_review.cross_verify(tree, nl)
        gates["assembly"] = asm["counts"]
        gates["cross_verify"] = xv["counts"]
        for f in asm["findings"] + xv["findings"]:
            line = f"{f['rule']} {f['ref']}: {f['message'][:140]}"
            if f["severity"] == "error":
                blockers.append(line)
            elif f["severity"] == "warning":
                warnings.append(line)

        def gate(name, res, keep=()):
            gates[name] = {**res["counts"], **{k: res[k] for k in keep}}
            for f in res["findings"]:
                line = f"{f['rule']} {f['ref']}: {f['message'][:140]}"
                if f["severity"] == "error":
                    blockers.append(line)
                elif f["severity"] == "warning":
                    warnings.append(line)

        if preset:
            from kicad_claude.adapters import dfm

            res = dfm.analyze(tree, preset, _preset_rules(preset))
            gate("dfm", res, ("dfm_score", "not_checked"))
        if loads:
            from kicad_claude.adapters import board_thermal, part_ratings
            from kicad_claude.tools.common import specs_by_ref

            specs = specs_by_ref(nl)
            ratings = part_ratings.check(nl, rev["power"], specs, loads)
            gate("thermal", board_thermal.analyze(tree, nl, specs, ratings,
                                                  ratings["assumptions"]["ambient_c"],
                                                  ratings["assumptions"]["tj_margin_c"]))

        folder = _fab_dir(fab_dir)
        if folder.is_dir():
            g = gerber_review.analyze(folder, _preset_rules(preset), state.get_active_board_path())
            gates["gerbers"] = {"folder": str(folder), **g["counts"],
                                "min_conductor_width_mm": g["min_conductor_width_mm"],
                                "min_drill_mm": g["min_drill_mm"]}
            blockers += [f"{f['rule']}: {f['message'][:140]}" for f in g["findings"]
                         if f["severity"] == "error"]
            for f in g["findings"]:
                if f["severity"] == "warning":
                    (blockers if f["rule"] == "GB-STALE" else warnings).append(
                        f"{f['rule']}: {f['message'][:140]}")
        else:
            blockers.append(f"Gerbers: no folder at {folder} (export_fab_package first)")

        if check_lifecycle:
            lc = audit_lifecycle(netlist_path=netlist_path)
            gates["lifecycle"] = lc["counts"]
            blockers += [f"{f['rule']} {f['ref']}: {f['message'][:140]}" for f in lc["findings"]
                         if f["severity"] == "error"]
            warnings += [f"{f['rule']} {f['ref']}: {f['message'][:140]}" for f in lc["findings"]
                         if f["severity"] == "warning"]
        return {"verdict": "NOT FAB-READY" if blockers else "FAB-READY",
                "blockers": blockers, "warnings": warnings, "gates": gates,
                "preset": preset or None}
