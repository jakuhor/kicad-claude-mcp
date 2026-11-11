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
        fa, fb = fkeys(a), fkeys(b)
        fmt = lambda s: [{"rule": r, "ref": ref, "message": m} for r, ref, m in sorted(s)]  # noqa: E731
        out = {"applied": applied, "regulators": reg_changes, "rails": rail_changes,
               "dividers": div_changes, "rc_filters": rc_changes,
               "findings_new": fmt(fb - fa), "findings_gone": fmt(fa - fb)}
        if filters:
            if not spice_auto.find_ngspice():
                out["filters"] = "skipped: ngspice not on PATH"
            else:
                freqs = sorted({k * f for f in (fsw or {}).values() for k in (1, 2, 3)})

                def ladders(nl, r):
                    res = spice_auto.analyze(
                        nl, ("lc",), volts={k: v["volts"] for k, v in
                                            r["power"]["rail_voltages"].items()},
                        regulators=r["power"]["regulators"],
                        sources=connectivity_review.sourced_nets(nl), freqs=freqs,
                        loads=loads or {})
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
                        max_parts: int = 200, netlist_path: str = "") -> dict:
        """Lifecycle and stock of every MPN in the active schematic at each source
        in `sources` (exact MPN match, needs their API keys). Findings: LC-EOL
        (error), LC-NRND, LC-RESTRICTED, LC-STOCK (no stock anywhere),
        LC-SINGLE, LC-DIST-DROP, LC-NOTFOUND.

        Details:

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

        names = clean(sources, ("digikey", "mouser", "farnell"))
        nl = power_review.parse_netlist(project_netlist(netlist_path))
        mpns: dict[str, list[str]] = {}
        for ref, comp in nl.components.items():
            m = (comp["fields"].get(mpn_field) or "").strip()
            if m:
                mpns.setdefault(m, []).append(ref)
        findings, parts, errors = [], [], {}
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
                        "stock", "unit_price", "currency") if k in h}
            parts.append(rec)
            refs = ",".join(rec["refs"][:6])
            texts = " | ".join(f"{s}: {v.get('lifecycle')}" for s, v in rec["sources"].items()
                               if v.get("lifecycle"))
            flags = any(v.get("discontinued") or v.get("end_of_life") for v in rec["sources"].values())
            if not rec["sources"]:
                findings.append({"severity": "info", "rule": "LC-NOTFOUND", "ref": refs,
                                 "message": f"{mpn}: no exact match at {', '.join(names)}."})
                continue
            if flags or EOL_RE.search(texts):
                findings.append({"severity": "error", "rule": "LC-EOL", "ref": refs,
                                 "message": f"{mpn}: {texts or 'discontinued / end of life'}."})
            elif NRND_RE.search(texts):
                findings.append({"severity": "warning", "rule": "LC-NRND", "ref": refs,
                                 "message": f"{mpn}: {texts}."})
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
        order = {"error": 0, "warning": 1, "info": 2}
        findings.sort(key=lambda f: (order[f["severity"]], f["rule"], f["ref"]))
        return {"parts": parts, "findings": findings, "lookup_errors": errors,
                "counts": {k: sum(1 for f in findings if f["severity"] == k) for k in order},
                "mpn_count": len(mpns), "checked": min(len(mpns), max_parts)}

    @mcp.tool()

    @on_board
    def review_assembly(assembly_sides: Literal["top", "bottom", "both"] = "top", min_spacing_mm: float = 0.0,
                        edge_clearance_mm: float = 0.3, fiducials_min: int = 2,
                        tall_mm: float = 5.0, tall_edge_mm: float = 3.0,
                        max_height_top_mm: float | None = None,
                        max_height_bottom_mm: float | None = None,
                        heights: bool = True,
                        cross_verify: bool = True, netlist_path: str = "") -> dict:
        """Assembly review of the active board plus schematic <-> PCB
        cross-verify: courtyard overlap, spacing and edge distance
        (`min_spacing_mm`, `edge_clearance_mm`), missing courtyards, SMD on an
        unpopulated side (`assembly_sides`), THT, fiducials, polarity and pin-1
        marks; `heights` from 3D models (`tall_mm`, `max_height_*_mm`); netlist
        vs board. Rule IDs AS-*, XV-*. DNP parts skipped.

        Details:

        Assembly: real courtyard outlines (non-convex ones too) that overlap
        (AS-CRTYD-OVERLAP; touching is fine) or sit closer than
        `min_spacing_mm` (AS-CRTYD-SPACING), courtyards beyond or within
        `edge_clearance_mm` of the edge (AS-EDGE; info for connectors),
        footprints with no courtyard (AS-NO-CRTYD), SMD parts on a side the
        assembler will not populate (`assembly_sides` top|bottom|both;
        AS-SIDE, error), through-hole parts (AS-THT), fiducials per populated
        side (AS-FIDUCIAL), polarized parts with no silkscreen of their own
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
        if assembly_sides not in ("top", "bottom", "both"):
            raise ValueError("assembly_sides must be top, bottom or both")
        tree = sch_io.parse_file(state.get_active_board_path())
        out = assembly_review.check_assembly(tree, assembly_sides, min_spacing_mm,
                                             edge_clearance_mm, fiducials_min, tall_mm,
                                             tall_edge_mm, max_height_top_mm,
                                             max_height_bottom_mm, heights)
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
                         assembly_sides: Literal["top", "bottom", "both"] = "top", netlist_path: str = "") -> dict:
        """One release verdict for the active project, FAB-READY or NOT FAB-READY,
        with blockers and warnings. Gates: ERC, DRC (no refill), power-tree and
        connectivity errors, Gerber folder vs `preset`, BOM MPN coverage
        (`require_mpn`), assembly review + cross-verify, optional lifecycle
        audit. Warnings never block. Read-only.

        Details:

        Gates (each reported with its numbers): ERC errors; DRC errors,
        unconnected items and schematic parity (run without refilling or
        saving the board); `analyze_power_tree` and `review_connectivity`
        errors; Gerber folder (`analyze_gerbers` errors, stale Gerbers, and
        the `preset` minimums); MPN coverage of the BOM (blocker only with
        `require_mpn`; test points, holes and DNP parts are exempt);
        `review_assembly` + cross-verify (their errors block: SMD on an
        unpopulated side, symbol pins without pads, parts missing from the
        board); optionally `audit_lifecycle` (LC-EOL blocks). Warnings never
        block.
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

        tree = sch_io.parse_file(state.get_active_board_path())
        asm = assembly_review.check_assembly(tree, assembly_sides)
        xv = assembly_review.cross_verify(tree, nl)
        gates["assembly"] = asm["counts"]
        gates["cross_verify"] = xv["counts"]
        for f in asm["findings"] + xv["findings"]:
            line = f"{f['rule']} {f['ref']}: {f['message'][:140]}"
            if f["severity"] == "error":
                blockers.append(line)
            elif f["severity"] == "warning":
                warnings.append(line)

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
