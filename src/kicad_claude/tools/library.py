"""Phase 2 — KiCAD library indexing and search tools.

Tools:
    index_libraries      — build (or load cached) index of KiCAD libs
    list_libraries       — list indexed symbol/footprint libraries with counts
    search_symbol        — fuzzy search across indexed symbols
    search_footprint     — fuzzy search across indexed footprints
    get_symbol_details   — full metadata + pin list for a specific symbol
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from kicad_claude import state
from kicad_claude.adapters import sch_io
from kicad_claude.indexer.kicad_libs import (
    build_index,
    cache_path,
    get_symbol_pins,
    load_cache,
    save_cache,
)
from kicad_claude.indexer.search import search_footprints, search_symbols
from kicad_claude.utils import kicad_config
from kicad_claude.utils.kicad_paths import find_footprint_lib_dirs, find_symbol_lib_dirs
from kicad_claude.utils.kicad_strings import normalize_name

logger = logging.getLogger("kicad-claude.tools.library")

# In-process memoization of the loaded index. The on-disk cache is the source
# of truth; this avoids re-reading the multi-MB JSON on every tool call.
_index: dict[str, Any] | None = None


def _lib_table_dirs(table_path: Path, project_dir: Path, suffix: str) -> list[Path]:
    """Directories named by a `sym-lib-table` / `fp-lib-table`.

    A symbol library's uri is the `.kicad_sym` file and a footprint library's
    is the `.pretty` folder; the indexer walks *directories*, so the parent is
    returned in both cases. `${KIPRJMOD}` is the project directory; every other
    path variable is resolved the way KiCAD resolves it, from
    `kicad_common.json` and the environment.
    """
    if not table_path.is_file():
        return []
    try:
        table = sch_io.parse_file(table_path)
    except Exception:  # noqa: BLE001 — a malformed table must not break indexing
        logger.warning("malformed %s; ignoring", table_path)
        return []

    variables = {**kicad_config.path_vars(), "KIPRJMOD": str(project_dir)}
    out: list[Path] = []
    for lib in sch_io.find_children(table, "lib"):
        uri_node = sch_io.find_child(lib, "uri")
        if not uri_node or len(uri_node) < 2:
            continue
        expanded = kicad_config.expand_uri(str(uri_node[1]), variables)
        if expanded is None:
            continue  # a path variable nothing defines; there is no dir to walk
        p = Path(expanded)
        if p.suffix.lower() != suffix:
            continue
        if p.parent.is_dir():
            out.append(p.parent)
    return out


def _project_lib_dirs() -> tuple[list[Path], list[Path]]:
    """(symbol dirs, footprint dirs) of the active project, if there is one.

    `create_symbol`, `create_footprint` and `import_vendor_zip` write into
    `<project>/lib` and register the library in the project's lib-table. Those
    libraries are not in any global directory, so they are picked up here —
    otherwise the server could not place a symbol it had just created.
    """
    proj = state.get_active_or_none()
    if proj is None:
        return [], []
    sym_dirs: list[Path] = []
    fp_dirs: list[Path] = []
    lib_dir = proj.path / "lib"
    if lib_dir.is_dir():
        sym_dirs.append(lib_dir)
        fp_dirs.append(lib_dir)
    sym_dirs += _lib_table_dirs(proj.path / "sym-lib-table", proj.path, ".kicad_sym")
    fp_dirs += _lib_table_dirs(proj.path / "fp-lib-table", proj.path, ".pretty")
    # Deduplicate, keeping order.
    return list(dict.fromkeys(sym_dirs)), list(dict.fromkeys(fp_dirs))


def _with_project_libs(idx: dict[str, Any]) -> dict[str, Any]:
    """Overlay the active project's own libraries on the global index.

    Read fresh on every call rather than cached: a project library is small,
    and it changes under the server's own hands.
    """
    sym_dirs, fp_dirs = _project_lib_dirs()
    if not sym_dirs and not fp_dirs:
        return idx
    extra = build_index(symbol_dirs=sym_dirs, footprint_dirs=fp_dirs)
    if not extra["symbols"] and not extra["footprints"]:
        return idx
    return {
        **idx,
        "symbols": {**idx.get("symbols", {}), **extra["symbols"]},
        "footprints": {**idx.get("footprints", {}), **extra["footprints"]},
        "symbol_dirs": list(dict.fromkeys(
            [str(d) for d in sym_dirs] + list(idx.get("symbol_dirs", []))
        )),
        "footprint_dirs": list(dict.fromkeys(
            [str(d) for d in fp_dirs] + list(idx.get("footprint_dirs", []))
        )),
    }


def _ensure_index() -> dict[str, Any]:
    global _index
    if _index is None:
        _index = load_cache()
        if _index is None:
            raise RuntimeError(
                "Library index not built. Call `index_libraries` first."
            )
    return _with_project_libs(_index)


def _invalidate() -> None:
    global _index
    _index = None


def _summary(index: dict[str, Any], from_cache: bool) -> dict[str, Any]:
    sym_libs = {s["lib"] for s in index["symbols"].values()}
    fp_libs = {f["lib"] for f in index["footprints"].values()}
    return {
        "from_cache": from_cache,
        "indexed_at": index.get("indexed_at"),
        "cache_path": str(cache_path()),
        "symbol_libraries": len(sym_libs),
        "symbols": len(index["symbols"]),
        "footprint_libraries": len(fp_libs),
        "footprints": len(index["footprints"]),
        "symbol_dirs": index.get("symbol_dirs", []),
        "footprint_dirs": index.get("footprint_dirs", []),
    }


# Fields that are prose for the caller to read, so KiCAD's `{brace}` escapes get
# decoded. `lib_id` and `name` are identity — callers hand them straight back to
# `get_symbol_details` and `add_symbol`, and `lib_symbols_has` compares them
# against the tree, so those stay exactly as the library spells them (P7).
READABLE_FIELDS = ("description", "keywords", "tags", "datasheet", "default_footprint")


def _decode_readable(entry: dict) -> dict:
    """Return `entry` with its prose fields brace-decoded, identity untouched."""
    out = dict(entry)
    for field in READABLE_FIELDS:
        value = out.get(field)
        if isinstance(value, str):
            out[field] = normalize_name(value)
    return out

def register(mcp) -> None:
    """Register Phase 2 tools on the FastMCP instance."""

    @mcp.tool()
    def index_libraries(force: bool = False) -> dict:
        """Build or refresh the KiCAD library index.

        On first run (or when `force=True`), walks the configured KiCAD
        symbol/footprint directories, parses every `.kicad_sym` and reads the
        header of every `.kicad_mod`, and writes a cache to
        `~/.cache/kicad-claude/index.json`.

        Subsequent calls return the cached summary instantly.

        The active project's own libraries (`<project>/lib` and whatever its
        `sym-lib-table` / `fp-lib-table` names) are read on every lookup, not
        cached, so a symbol created by `create_symbol` can be placed at once.

        Refuses to save an index that found no symbol at all, rather than
        replacing a good cache with an empty one: that reads as "every library
        is gone" and is usually a path problem, not an empty machine.
        """
        global _index
        if not force:
            cached = load_cache()
            if cached is not None:
                _index = cached
                return _summary(_with_project_libs(cached), from_cache=True)

        logger.info("building library index from scratch")
        idx = build_index()
        if not idx.get("symbols") and not idx.get("footprints"):
            searched = [str(d) for d in (find_symbol_lib_dirs() + find_footprint_lib_dirs())]
            raise RuntimeError(
                "no KiCAD libraries found, so the index was not written "
                f"(the previous cache is untouched). Searched: {searched or 'nothing'}. "
                "Set KICAD_LIBRARY_PATH to the directories holding .kicad_sym "
                "files and .pretty folders, separated by "
                f"{os.pathsep!r}."
            )
        save_cache(idx)
        _index = idx
        return _summary(_with_project_libs(idx), from_cache=False)

    @mcp.tool()
    def list_libraries() -> dict:
        """List indexed libraries with per-library entry counts.

        Returns:
            symbol_libraries: list of {name, count}, sorted by name
            footprint_libraries: same shape
        """
        idx = _ensure_index()
        sym_counts: dict[str, int] = {}
        for s in idx["symbols"].values():
            sym_counts[s["lib"]] = sym_counts.get(s["lib"], 0) + 1
        fp_counts: dict[str, int] = {}
        for f in idx["footprints"].values():
            fp_counts[f["lib"]] = fp_counts.get(f["lib"], 0) + 1

        return {
            "symbol_libraries": sorted(
                [{"name": k, "count": v} for k, v in sym_counts.items()],
                key=lambda x: x["name"],
            ),
            "footprint_libraries": sorted(
                [{"name": k, "count": v} for k, v in fp_counts.items()],
                key=lambda x: x["name"],
            ),
        }

    @mcp.tool()
    def search_symbol(query: str, max_results: int = 10) -> list[dict]:
        """Fuzzy search across indexed symbols by lib_id, description, keywords.

        Returns up to `max_results` ranked matches. Each entry includes
        `_score` (0–100) so the caller can judge match confidence.
        """
        idx = _ensure_index()
        return [
            _decode_readable(e)
            for e in search_symbols(query, idx, max_results=max_results)
        ]

    @mcp.tool()
    def search_footprint(query: str, max_results: int = 10) -> list[dict]:
        """Fuzzy search across indexed footprints by lib_id, description, tags."""
        idx = _ensure_index()
        return [
            _decode_readable(e)
            for e in search_footprints(query, idx, max_results=max_results)
        ]

    @mcp.tool()
    def get_symbol_details(lib_id: str) -> dict:
        """Return full metadata and pin list for a symbol by `lib_id`.

        Re-parses the source `.kicad_sym` to extract pins (number, name,
        electrical type, shape) — the index only stores pin counts.
        """
        idx = _ensure_index()
        meta = idx["symbols"].get(lib_id)
        if meta is None:
            raise KeyError(f"unknown symbol lib_id: {lib_id!r}")

        pins: list[dict] = []
        for d_str in idx.get("symbol_dirs", []):
            lib_path = Path(d_str) / f"{meta['lib']}.kicad_sym"
            if lib_path.is_file():
                pins = get_symbol_pins(lib_path, meta["name"])
                break

        pins = [
            {**pin, "name": normalize_name(pin["name"])}
            if isinstance(pin.get("name"), str) else pin
            for pin in pins
        ]
        return {**_decode_readable(meta), "pins": pins}
