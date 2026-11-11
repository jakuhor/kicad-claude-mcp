"""Project datasheet tools.

Tools:
    sync_datasheets — one PDF per schematic MPN in `<project>/datasheets/`,
                      matched to existing vendor-named files or downloaded,
                      recorded in `manifest.kicad-claude.json`
    get_datasheet_extraction_task — PDF path, key pages' text, schematic
                      context and schema for the agent to extract specs
    save_datasheet_extraction — validate (units, page cites, numbers found
                      on the cited page) and cache `datasheets/specs/<MPN>.json`
    get_datasheet_specs — one MPN's cached specs, or coverage of all
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path

from kicad_claude import state
from kicad_claude.arg_types import Source, clean
from kicad_claude.adapters import datasheet_fetch, datasheet_index as dsi, power_review
from kicad_claude.adapters import datasheet_specs as dss
from kicad_claude.tools.common import project_netlist

logger = logging.getLogger("kicad-claude.tools.datasheets")

DEFAULT_SKIP = ("R", "C", "TP", "H", "MH", "FID", "NT", "JP")


def _ds_dir(dest_dir: str) -> Path:
    if dest_dir:
        return Path(dest_dir).expanduser().resolve()
    return dsi.datasheets_dir(state.get_active().path)


def _natural(ref: str) -> tuple:
    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", ref))


def schematic_parts(netlist: Path, mpn_field: str, skip_prefixes) -> tuple[dict, list]:
    """MPN -> {refs, value, manufacturer, datasheet}; plus refs without an MPN."""
    skip = {s.upper() for s in clean(skip_prefixes)}
    nl = power_review.parse_netlist(netlist)
    parts: dict[str, dict] = {}
    no_mpn: list[str] = []
    for ref, comp in sorted(nl.components.items(), key=lambda kv: _natural(kv[0])):
        if power_review.kind(ref).upper() in skip:
            continue
        mpn = (comp["fields"].get(mpn_field) or "").strip()
        if not mpn:
            no_mpn.append(ref)
            continue
        p = parts.setdefault(mpn, {"refs": [], "value": comp["value"],
                                   "manufacturer": comp["fields"].get("Manufacturer", ""),
                                   "datasheet": ""})
        p["refs"].append(ref)
        ds = comp.get("datasheet", "")
        if ds and ds != "~" and not p["datasheet"]:
            p["datasheet"] = ds
    return parts, no_mpn


def register(mcp) -> None:
    @mcp.tool()
    def sync_datasheets(
        download: bool = True,
        sources: list[Source] | None = None,
        assign: dict[str, str] | None = None,
        skip_prefixes: list[str] | None = None,
        mpn_field: str = "MPN",
        dest_dir: str = "",
        netlist_path: str = "",
    ) -> dict:
        """Give every MPN in the active schematic its datasheet PDF, recorded in
        `datasheets/manifest.kicad-claude.json` (folder: `dest_dir`,
        `<project>/datasheets`, or the nearest parent `datasheets/`). Order:
        kept manifest entry, `assign={"MPN": "file.pdf"}`, Datasheet-URL base
        name, PDF text match, then `download=True`. Returns per-MPN status,
        `unresolved`, `alternatives` and `orphans`.

        Details:

        The folder is `dest_dir`, else `<project>/datasheets`, else the
        nearest `datasheets/` in a parent folder up to the git root (one
        shared folder for a multi-board repo).

        Per MPN (from the `mpn_field` symbol field; parts whose reference
        prefix is in `skip_prefixes` — R, C, test points, holes … — are left
        out), in order:
          1. kept: the manifest already maps it and the file still exists;
          2. `assign={"SMF24A": "TVS-SMF-1103.pdf"}` — explicit, wins over
             matching;
          3. an existing top-level PDF whose name is the base name of the
             symbol's Datasheet URL (`8471fd.pdf`);
          4. an existing PDF whose text contains the MPN, or its longest
             prefix down to the family stem (`XAL4020` in `xal4000.pdf`).
             Ties are reported under `alternatives` — resolve with `assign`;
          5. `download=True`: the symbol's Datasheet URL, then `sources`
             (same fallback and `%PDF` check as `fetch_datasheet`).

        Sub-folders (`ingest/`, `input/`) are never read. MPNs that left the
        schematic stay in the manifest with `in_schematic: false`. Returns
        per-MPN status, `no_mpn` refs, `unresolved` MPNs and `orphans` — top
        level PDFs no MPN maps to (candidates for `ingest/`).
        """
        ds_dir = _ds_dir(dest_dir)
        parts, no_mpn = schematic_parts(project_netlist(netlist_path), mpn_field,
                                       clean(skip_prefixes, DEFAULT_SKIP))
        manifest = dsi.load_manifest(ds_dir)
        files = dsi.pdf_files(ds_dir)
        by_name = {f.name: f for f in files}
        lower = {f.name.lower(): f.name for f in files}
        digests = {f.name: dsi.sha256(f) for f in files}

        status: dict[str, dict] = {}
        resolved: dict[str, tuple[str, str]] = {}   # mpn -> (file, how)
        for mpn in parts:
            entry = manifest["parts"].get(mpn)
            if entry and entry.get("file") in by_name and mpn not in (assign or {}):
                resolved[mpn] = (entry["file"], entry.get("how", "manifest"))
                status[mpn] = {"status": "kept"}
        for mpn, fname in (assign or {}).items():
            if mpn not in parts:
                raise ValueError(f"assign: {mpn!r} is not an MPN in the schematic")
            real = lower.get(fname.lower())
            if not real:
                raise ValueError(f"assign: {fname!r} is not a PDF in {ds_dir}")
            resolved[mpn] = (real, "assigned")
            status[mpn] = {"status": "assigned"}

        todo = {m: p for m, p in parts.items() if m not in resolved}
        for mpn, hit in dsi.match_existing(todo, files, digests).items():
            resolved[mpn] = (hit["file"], hit["how"])
            status[mpn] = {"status": "matched", "how": hit["how"], "score": hit["score"]}
            if hit["alternatives"]:
                status[mpn]["alternatives"] = hit["alternatives"]

        unresolved = []
        for mpn, info in parts.items():
            if mpn in resolved:
                continue
            if not download:
                unresolved.append(mpn)
                status[mpn] = {"status": "missing"}
                continue
            from kicad_claude.tools.sourcing.distributors import DATASHEET_SOURCES, datasheet_candidates

            url = info["datasheet"] if re.match(r"(?i)(https?:)?//", info["datasheet"]) else ""
            cands, errors = datasheet_candidates(mpn, clean(sources, DATASHEET_SOURCES), url)
            res = datasheet_fetch.fetch_first_pdf(cands, mpn, ds_dir)
            if res["path"]:
                f = Path(res["path"])
                digests[f.name] = dsi.sha256(f)
                found = dsi.content_score(mpn, dsi.norm(" ".join(dsi.pdf_pages(f, digests[f.name]))))
                resolved[mpn] = (f.name, f"download:{res['source']}")
                status[mpn] = {"status": "downloaded" if res["saved"] else "matched",
                               "source": res["source"], "url": res["url"],
                               "mpn_in_text": bool(found)}
                manifest["parts"].setdefault(mpn, {})["url"] = res["url"]
            else:
                unresolved.append(mpn)
                status[mpn] = {"status": "failed", "attempts": res["attempts"],
                               "lookup_errors": errors}

        ts = dsi.now()
        for mpn, (fname, how) in resolved.items():
            e = manifest["parts"].setdefault(mpn, {})
            changed = e.get("file") != fname or e.get("sha256") != digests[fname]
            e.update({"file": fname, "sha256": digests[fname], "how": how,
                      "refs": parts[mpn]["refs"], "value": parts[mpn]["value"],
                      "manufacturer": parts[mpn]["manufacturer"],
                      "in_schematic": True})
            if parts[mpn]["datasheet"]:
                e.setdefault("url", parts[mpn]["datasheet"])
            if changed or "updated" not in e:
                e["updated"] = ts
            status[mpn].update({"file": fname, "refs": parts[mpn]["refs"]})
        for mpn, e in manifest["parts"].items():
            if mpn not in parts:
                e["in_schematic"] = False
        mpath = dsi.save_manifest(ds_dir, manifest)

        used = {f for f, _ in resolved.values()}
        files_now = dsi.pdf_files(ds_dir)
        by_file = defaultdict(list)
        for mpn, (f, _) in resolved.items():
            by_file[f].append(mpn)
        return {
            "datasheets_dir": str(ds_dir),
            "manifest": str(mpath),
            "counts": {k: sum(1 for s in status.values() if s["status"] == k)
                       for k in ("kept", "assigned", "matched", "downloaded", "missing", "failed")},
            "parts": dict(sorted(status.items())),
            "unresolved": sorted(unresolved),
            "no_mpn": no_mpn,
            "orphans": sorted(f.name for f in files_now if f.name not in used),
            "shared_files": {f: sorted(m) for f, m in by_file.items() if len(m) > 1},
        }

    def _entry(ds_dir: Path, mpn: str) -> tuple[dict, Path]:
        man = dsi.load_manifest(ds_dir)
        e = man["parts"].get(mpn)
        if not e:
            raise ValueError(f"{mpn!r} is not in {dsi.MANIFEST_NAME}; run sync_datasheets first")
        # The manifest may come from a cloned repo: its `file` must name a PDF
        # directly inside the datasheets folder, as sync_datasheets writes it.
        name = str(e.get("file") or "")
        pdf = ds_dir / name
        if (not name or Path(name).name != name or name in (".", "..")
                or pdf.resolve().parent != ds_dir.resolve()):
            raise ValueError(f"manifest entry for {mpn} points outside {ds_dir}: {name!r}")
        if not pdf.is_file():
            raise FileNotFoundError(f"{pdf} (manifest entry for {mpn}); rerun sync_datasheets")
        return e, pdf

    @mcp.tool()
    def get_datasheet_extraction_task(
        mpn: str,
        pages: str = "",
        max_chars: int = 40000,
        dest_dir: str = "",
        netlist_path: str = "",
    ) -> dict:
        """Everything needed to extract `mpn`'s datasheet into a spec record.

        Returns the absolute PDF path (read it — tables and graphs often do not
        survive text extraction), page count, the text of the pages most
        likely to hold pinout / abs-max / recommended / electrical tables
        (or `pages="3,5-7"`), up to `max_chars`, the schematic context (refs,
        each ref's pin names and nets), the schema and the rules.

        Then call `save_datasheet_extraction(mpn, extraction)`.
        """
        ds_dir = _ds_dir(dest_dir)
        e, pdf = _entry(ds_dir, mpn)
        all_pages = dsi.pdf_pages(pdf, e.get("sha256"))
        if pages:
            sel = []
            for part in pages.split(","):
                a, _, z = part.strip().partition("-")
                sel += list(range(int(a), int(z or a) + 1))
            sel = [i for i in sel if 1 <= i <= len(all_pages)]
        else:
            sel = dss.select_pages(all_pages)
        text, used, budget = [], [], max_chars
        for i in sel:
            t = all_pages[i - 1]
            if budget <= 0:
                break
            text.append({"page": i, "text": t[:budget]})
            used.append(i)
            budget -= len(t)
        context = {}
        try:
            nl = power_review.parse_netlist(project_netlist(netlist_path))
            for ref in e.get("refs", []):
                context[ref] = {p.number: {"name": p.name, "net": p.net}
                                for p in nl.pins.get(ref, [])}
        except Exception as ex:  # noqa: BLE001 - context is a convenience
            context = {"error": str(ex)}
        existing = dss.load(ds_dir, mpn)
        return {
            "mpn": mpn, "pdf": str(pdf), "page_count": len(all_pages),
            "pages": text, "pages_included": used,
            "pages_suggested": dss.select_pages(all_pages, limit=15),
            "schematic": {"refs": e.get("refs", []), "value": e.get("value", ""),
                          "manufacturer": e.get("manufacturer", ""), "pins": context},
            "existing_extraction": existing,
            "schema": dss.schema_text(),
            "categories": list(dss.CATEGORIES), "units": sorted(dss.UNITS),
            "rules": [
                "Extract only what the datasheet states; never fill a value from general knowledge.",
                "Every spec value cites its 1-based PDF page; add `quote` when the row text is short.",
                "SI base units: 500 kHz -> 500000 Hz, 75 mV -> 0.075 V, 2 uA -> 2e-06 A.",
                "Use the variant matching the MPN (package, fixed/adjustable, grade).",
                "Multi-channel parts: per-channel values under regulator.channels.",
                "Canonical keys where they apply (analyzers read them): absolute_max / "
                "recommended VIN, VOUT, EN (also for EN/UV, OV/UV, SHDN pins), IOUT, TJ; "
                "regulator vref, vref_negative, set_current, anyout, dropout, "
                "en_threshold_rising, en_threshold_falling, en_active, pg_output, fsw.",
                "Omit a field rather than guess; put caveats in notes.",
            ],
        }

    @mcp.tool()
    def save_datasheet_extraction(
        mpn: str,
        extraction: dict,
        extracted_by: str = "calling agent",
        overwrite: bool = False,
        dest_dir: str = "",
    ) -> dict:
        """Validate a spec record for `mpn` and cache it in
        `<project>/datasheets/specs/<MPN>.json`.

        Rejected (nothing written) on structural errors: unknown keys or
        category, non-SI unit, missing page, min > typ / typ > max, page
        out of range, bad I2C address. Saved with `warnings` when a number is
        not found in the cited page's text (`unverified` — re-check that page;
        tables may not extract). The record stores the PDF hash, so a changed
        PDF marks it `stale`. An existing record is kept unless `overwrite`.
        """
        ds_dir = _ds_dir(dest_dir)
        e, pdf = _entry(ds_dir, mpn)
        if dss.specs_path(ds_dir, mpn).exists() and not overwrite:
            raise FileExistsError(f"specs for {mpn} exist; pass overwrite=True to replace")
        sha = dsi.sha256(pdf)
        errors, warnings = dss.validate(extraction, dsi.pdf_pages(pdf, sha))
        if extraction.get("mpn") not in (None, mpn):
            errors.append(f"mpn: record says {extraction.get('mpn')!r}, call says {mpn!r}")
        if errors:
            return {"saved": False, "errors": errors, "warnings": warnings}
        path = dss.save(ds_dir, mpn, extraction, pdf.name, sha, extracted_by)
        return {"saved": True, "path": str(path), "warnings": warnings}

    @mcp.tool()
    def get_datasheet_specs(mpn: str = "", dest_dir: str = "") -> dict:
        """Cached spec record for `mpn` (with `stale` if its PDF changed).
        Without `mpn`: coverage — every manifest MPN with `extracted`,
        `stale`, `missing`, and the refs it serves."""
        ds_dir = _ds_dir(dest_dir)
        if mpn:
            rec = dss.load(ds_dir, mpn)
            if rec is None:
                return {"mpn": mpn, "found": False,
                        "hint": "get_datasheet_extraction_task then save_datasheet_extraction"}
            return {"found": True, **rec}
        man = dsi.load_manifest(ds_dir)
        rows = {}
        for m, e in man["parts"].items():
            if not e.get("in_schematic", True):
                continue
            rec = dss.load(ds_dir, m)
            rows[m] = {"refs": e.get("refs", []), "file": e.get("file"),
                       "state": "missing" if rec is None else "stale" if rec["stale"] else "extracted"}
        counts = {s: sum(1 for r in rows.values() if r["state"] == s)
                  for s in ("extracted", "stale", "missing")}
        return {"counts": counts, "parts": rows}
