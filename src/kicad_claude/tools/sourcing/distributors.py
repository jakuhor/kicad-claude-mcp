"""External sourcing tools.

Tools:
    check_availability        — distributor stock/price lookup by MPN
    find_or_fetch_symbol      — local index → KiCAD official → manual SnapEDA fallback
    import_vendor_zip         — extract a vendor ZIP into the active project's lib/
    list_vendor_parts         — list ZIPs available under ./vendor_parts/
    enrich_bom_with_sourcing  — augment a KiCAD BOM CSV with distributor data

Supported sources: mouser (default), digikey, tme, farnell, jlcpcb (keyless,
JLCPCB assembly library: LCSC number, Basic/Preferred/Extended, USD prices). Every tool that
talks to a distributor takes a `sources` string, e.g. "mouser,tme".
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from kicad_claude import state
from kicad_claude.arg_types import Source, clean
# farnell / mouser / tme are reached through `_adapter()` by name, so ruff
# cannot see the use — hence the noqa.
from kicad_claude.adapters import (
    digikey,
    farnell,  # noqa: F401
    jlcpcb,  # noqa: F401
    kicad_cli,
    mouser,  # noqa: F401
    snapeda,
    tme,  # noqa: F401
    vendor_import,
)
from kicad_claude.tools.library import index as lib_tools

logger = logging.getLogger("kicad-claude.tools.sourcing.distributors")

VENDOR_PARTS_DIR_NAME = "vendor_parts"

#: Every distributor the sourcing tools can query, in report order.
SOURCE_NAMES = ("digikey", "mouser", "tme", "farnell", "jlcpcb")

#: Default when the caller does not name any source. Mouser needs only a
#: single API key, so it is the one source that works out of the box.
DEFAULT_SOURCES = ("mouser",)

#: Datasheet lookups: Mouser last, it blocks automated downloads.
DATASHEET_SOURCES = ("digikey", "farnell", "jlcpcb", "mouser")

#: Column prefix used by `enrich_bom_with_sourcing`.
SOURCE_PREFIX = {"digikey": "dk", "mouser": "mo", "tme": "tme", "farnell": "fn",
                 "jlcpcb": "jlc"}

#: Extra per-source BOM columns beyond the common six: (column suffix, hit key).
EXTRA_COLUMNS = {"jlcpcb": (("lcsc", "lcsc"), ("type", "library_type"))}

_ERROR_ATTR = {
    "digikey": "DigiKeyError",
    "mouser": "MouserError",
    "tme": "TMEError",
    "farnell": "FarnellError",
    "jlcpcb": "JLCPCBError",
}


def _adapter(name: str):
    """Return the adapter module by source name.

    Looked up through `globals()` rather than captured at import time so
    tests can monkeypatch a single adapter.
    """
    return globals()[name]


def source_error(name: str) -> type[Exception]:
    return getattr(_adapter(name), _ERROR_ATTR[name])


def search_source(name: str, query: str, limit: int = 3) -> list[dict]:
    """Query one distributor. DigiKey has no part-number endpoint in V4."""
    mod = _adapter(name)
    if name == "digikey":
        return mod.search_keyword(query, limit=limit)
    return mod.search_part(query)[:limit]



def datasheet_candidates(mpn: str, sources, url: str = "") -> tuple[list[dict], dict]:
    """Datasheet URLs for `mpn`: `url` first, then each source's
    `datasheet_url` in the caller's order. Returns (candidates, lookup_errors)."""
    names = [s.lower() for s in clean(sources)]
    unknown = [n for n in names if n not in SOURCE_NAMES]
    if unknown:
        raise ValueError(f"Unknown source(s) {unknown}. Pick from {', '.join(SOURCE_NAMES)}.")
    candidates: list[dict] = []
    if url:
        candidates.append({"source": "url", "url": url})
    lookup_errors: dict[str, str] = {}
    for name in names:  # caller's order, not SOURCE_NAMES order
        try:
            hits = search_source(name, mpn, limit=3)
        except source_error(name) as e:
            lookup_errors[name] = str(e)
            continue
        exact = [h for h in hits if (h.get("mpn") or "").upper() == mpn.upper()]
        for h in exact or hits[:1]:
            if h.get("datasheet_url"):
                candidates.append({"source": name, "url": h["datasheet_url"]})
    return candidates, lookup_errors

def _parse_sources(sources) -> list[str]:
    """Validate a source list."""
    names = [s.lower() for s in clean(sources)]
    if not names:
        raise ValueError(
            f"No sources given. Pick from {', '.join(SOURCE_NAMES)}."
        )
    unknown = [n for n in names if n not in SOURCE_NAMES]
    if unknown:
        raise ValueError(
            f"Unknown source(s) {unknown}. Pick from {', '.join(SOURCE_NAMES)}."
        )
    # Keep SOURCE_NAMES order so output columns are stable.
    return [n for n in SOURCE_NAMES if n in names]


def _project_root_for_vendor_parts() -> Path:
    """Return the directory where vendor ZIPs live.

    Resolution order:
    1. Active project's `vendor_parts/` (if a project is set)
    2. Repo-root's `vendor_parts/` (server.py's parent)
    """
    proj = state.get_active_or_none()
    if proj and (proj.path / VENDOR_PARTS_DIR_NAME).exists():
        return proj.path / VENDOR_PARTS_DIR_NAME

    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / VENDOR_PARTS_DIR_NAME


def register(mcp) -> None:
    """Register the tools."""

    @mcp.tool()
    def check_availability(mpn: str, sources: list[Source] | None = None) -> dict:
        """Stock, price and links for `mpn` at each source in `sources` (digikey, mouser, tme, farnell, jlcpcb; default mouser). `jlcpcb`
        needs no key, accepts an LCSC number, and adds the LCSC code, basic /
        extended type and price breaks. Per-source failures are reported, not
        raised.

        Details:

        `sources` is a list of: digikey, mouser, tme, farnell,
        jlcpcb. The default is "mouser"; the others are opt-in. `jlcpcb` needs
        no key and adds `lcsc` (C-number), `library_type`
        (basic / preferred / extended — Basic parts carry no JLCPCB feeder
        fee), `package` and `price_breaks` (USD); it also accepts an LCSC
        number as `mpn`.

        Any source may fail (auth, network, no match) — failures are captured
        per-source so the caller still sees what worked. A source that was not
        requested stays `None`.
        """
        names = _parse_sources(clean(sources, DEFAULT_SOURCES))
        out: dict = {"mpn": mpn, "sources": names, "errors": {}}
        for name in SOURCE_NAMES:
            out[name] = None

        for name in names:
            try:
                hits = search_source(name, mpn, limit=3)
            except source_error(name) as e:
                out["errors"][name] = str(e)
                continue
            # Prefer an exact MPN match if present, else the first result.
            exact = next(
                (r for r in hits if (r.get("mpn") or "").upper() == mpn.upper()), None
            )
            out[name] = exact or (hits[0] if hits else None)

        return out

    @mcp.tool()
    def find_or_fetch_symbol(query: str, mpn: str | None = None) -> dict:
        """Locate a KiCAD lib_id for `query`. Cascade:

        1. Local + KiCAD official libs (via the indexer's fuzzy search)
        2. If `mpn` is given and not found locally, surface manufacturer info
           from DigiKey (so the caller knows what to download from SnapEDA)
        3. Returns a clear manual-import message if all else fails.

        This tool does NOT auto-download from SnapEDA (their public site
        requires login). The caller drops a ZIP into `vendor_parts/` and
        calls `import_vendor_zip`.
        """
        # 1. Local index search
        try:
            results = lib_tools._ensure_index()
        except RuntimeError:
            return {
                "found": False,
                "hint": "Library index not built. Call `index_libraries` first.",
            }

        from kicad_claude.indexer.search import search_symbols

        hits = search_symbols(query, results, max_results=5)
        if hits and hits[0]["_score"] >= 70:
            best = hits[0]
            return {
                "found": True,
                "source": "local_index",
                "lib_id": best["lib_id"],
                "description": best["description"],
                "pin_count": best["pin_count"],
                "default_footprint": best["default_footprint"],
                "alternatives": [h["lib_id"] for h in hits[1:]],
            }

        # 2. Try to enrich with manufacturer info from DigiKey
        manufacturer = None
        if mpn:
            try:
                dk = digikey.search_keyword(mpn, limit=1)
                if dk:
                    manufacturer = dk[0].get("manufacturer")
            except digikey.DigiKeyError as e:
                logger.info("DigiKey enrichment skipped: %s", e)

        # 3. Manual fallback
        return {
            "found": False,
            "query": query,
            "mpn": mpn,
            "best_local_match": (
                {"lib_id": hits[0]["lib_id"], "score": hits[0]["_score"]}
                if hits
                else None
            ),
            "manufacturer": manufacturer,
            "instructions": snapeda.manual_fallback_message(
                mpn or query,
                manufacturer,
                vendor_parts_dir=str(_project_root_for_vendor_parts()),
            ),
            "snapeda_url": snapeda.part_page_url(mpn or query, manufacturer),
        }

    @mcp.tool()
    def fetch_datasheet(
        mpn: str,
        sources: list[Source] | None = None,
        url: str = "",
        dest_dir: str = "",
        overwrite: bool = False,
    ) -> dict:
        """Download the datasheet PDF for `mpn`: `url` first, then each source's
        datasheet URL in `sources` order. Only `%PDF` bodies count; TLS is
        always verified. Saved to `dest_dir` (default `<project>/datasheets/`);
        an existing file is kept unless `overwrite=True`. `attempts` lists each
        failure.

        Details:

        Candidates, in order: `url` (if given), then each source's
        `datasheet_url` in `sources` order. Mouser is last by default because
        it blocks automated downloads. A candidate counts only when the body
        starts with `%PDF` — bot-protection HTML (Littelfuse) is rejected —
        and TLS verification is never turned off: an expired certificate
        (onsemi) is reported and the next candidate is tried.

        Saved to `dest_dir`, default `<project>/datasheets/`, under the
        vendor's file name when the URL ends in `.pdf`, else `<MPN>.pdf`. An
        existing file is kept unless `overwrite=True`. `attempts` lists every
        candidate with the reason it failed.
        """
        from kicad_claude.adapters import datasheet_fetch

        candidates, lookup_errors = datasheet_candidates(
            mpn, clean(sources, DATASHEET_SOURCES), url)

        if dest_dir:
            dest = Path(dest_dir).expanduser().resolve()
        else:
            proj = state.get_active_or_none()
            if proj is None:
                raise RuntimeError("no active project; pass dest_dir")
            dest = proj.path / "datasheets"
        result = datasheet_fetch.fetch_first_pdf(candidates, mpn, dest, overwrite=overwrite)
        result["mpn"] = mpn
        result["lookup_errors"] = lookup_errors
        if not result["path"]:
            result["hint"] = ("No candidate served a PDF. Download it by hand into "
                              f"{dest} or pass url= with a direct PDF link.")
        return result

    @mcp.tool()
    def import_vendor_zip(zip_path: str, target_lib: str = "vendor") -> dict:
        """Extract a vendor ZIP into `<active-project>/lib/{target_lib}.*`.

        Updates the project's `sym-lib-table` and `fp-lib-table` so KiCAD sees
        the new library. After import, call `index_libraries(force=True)` to
        refresh the searchable index (the local libs aren't auto-watched).
        """
        proj = state.get_active()
        result = vendor_import.import_zip(
            Path(zip_path), proj.path, target_lib=target_lib
        )
        # Hint the caller to refresh the index so search_symbol can find the new entries.
        result["next_step"] = "Call index_libraries(force=True) to make new lib_ids searchable."
        return result

    @mcp.tool()
    def enrich_bom_with_sourcing(
        bom_path: str | None = None,
        output_path: str | None = None,
        sourcing_field: str = "Value",
        sources: list[Source] | None = None,
        max_rows: int = 200,
    ) -> dict:
        """Add distributor stock and price columns to a KiCAD BOM CSV (`bom_path`,
        else a fresh export to `<project>/fab/`). Queries each unique
        `sourcing_field` value (default "Value"; use "MPN" when present) at
        `sources` (digikey, mouser, tme, farnell, jlcpcb; default
        mouser): six prefixed columns per source. `max_rows` caps API calls.

        Details:

        `sources` is a list of: digikey, mouser, tme, farnell,
        jlcpcb. The default is "mouser"; the others are opt-in. `jlcpcb` adds
        two columns, `jlc_lcsc` and `jlc_type` (basic / preferred / extended).

        For each unique value in `sourcing_field` (default "Value", but pass
        "MPN" if your schematic carries that custom field), this queries every
        requested source and appends six columns per source, prefixed
        dk_ (digikey), mo_ (mouser), tme_ (tme), fn_ (farnell) or jlc_ (jlcpcb):

            <prefix>_mpn, <prefix>_manufacturer, <prefix>_stock,
            <prefix>_price, <prefix>_currency, <prefix>_url

        Empty values for components that aren't real parts (e.g. "10k") are
        normal — the API returns no match and the columns stay blank.

        If `bom_path` is None, exports a fresh BOM with `kicad-cli sch export
        bom` first into `<project>/fab/`. `max_rows` caps API calls (DigiKey
        rate-limits free tier).
        """
        proj = state.get_active()

        # Resolve / generate BOM
        if bom_path is None:
            bom = proj.path / "fab" / f"{proj.name}-bom.csv"
            bom.parent.mkdir(parents=True, exist_ok=True)
            kicad_cli.export_bom(proj.sch_path, bom)
        else:
            bom = Path(bom_path).expanduser().resolve()
            if not bom.is_file():
                raise FileNotFoundError(bom)

        out = (
            Path(output_path).expanduser()
            if output_path
            else proj.path / "fab" / f"{proj.name}-bom-enriched.csv"
        )
        out.parent.mkdir(parents=True, exist_ok=True)

        names = _parse_sources(clean(sources, DEFAULT_SOURCES))

        # Read BOM (KiCAD writes UTF-8 with default delimiter ',')
        with bom.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fieldnames = list(reader.fieldnames or [])

        if sourcing_field not in fieldnames:
            raise ValueError(
                f"BOM has no field {sourcing_field!r}. Columns: {fieldnames}. "
                f"Re-run export(kind='bom') with a `fields` option including the field, or pass "
                f"sourcing_field with one of the existing columns."
            )

        # Cache lookups by query so we don't hit the API multiple times for
        # the same value. One cache per source.
        caches: dict[str, dict[str, dict]] = {name: {} for name in names}
        errors: dict[str, str] = {}

        for name in names:
            prefix = SOURCE_PREFIX[name]
            suffixes = ["mpn", "manufacturer", "stock", "price", "currency", "url"]
            suffixes += [c for c, _k in EXTRA_COLUMNS.get(name, ())]
            for suffix in suffixes:
                col = f"{prefix}_{suffix}"
                if col not in fieldnames:
                    fieldnames.append(col)

        api_calls = 0
        for row in rows[:max_rows]:
            query = (row.get(sourcing_field) or "").strip().strip('"')
            if not query:
                continue

            for name in names:
                cache = caches[name]
                if query not in cache:
                    try:
                        results = search_source(name, query, limit=1)
                        cache[query] = results[0] if results else {}
                        api_calls += 1
                    except source_error(name) as e:
                        errors.setdefault(name, str(e))
                        cache[query] = {}

                hit = cache.get(query, {})
                prefix = SOURCE_PREFIX[name]
                row[f"{prefix}_mpn"] = hit.get("mpn", "")
                row[f"{prefix}_manufacturer"] = hit.get("manufacturer", "")
                row[f"{prefix}_stock"] = hit.get("stock", "")
                row[f"{prefix}_price"] = hit.get("unit_price", "")
                row[f"{prefix}_currency"] = hit.get("currency", "")
                row[f"{prefix}_url"] = hit.get("product_url", "")
                for col, key in EXTRA_COLUMNS.get(name, ()):
                    row[f"{prefix}_{col}"] = hit.get(key, "")

        # Write the enriched CSV
        with out.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

        # Summary stats
        result: dict = {
            "input_bom": str(bom),
            "output_path": str(out),
            "row_count": len(rows),
            "sources": names,
            "unique_queries": max((len(c) for c in caches.values()), default=0),
            "api_calls": api_calls,
            "errors": errors,
        }
        for name in SOURCE_NAMES:
            cache = caches.get(name)
            result[f"{name}_hits"] = (
                sum(1 for v in cache.values() if v) if cache is not None else None
            )
        return result

    @mcp.tool()
    def list_vendor_parts() -> dict:
        """List ZIP files available under the `vendor_parts/` drop directory."""
        d = _project_root_for_vendor_parts()
        if not d.is_dir():
            return {"directory": str(d), "count": 0, "zips": []}
        zips = sorted(
            (
                {
                    "name": z.name,
                    "path": str(z),
                    "size_kb": round(z.stat().st_size / 1024, 1),
                }
                for z in d.glob("*.zip")
            ),
            key=lambda x: x["name"],
        )
        return {"directory": str(d), "count": len(zips), "zips": zips}
