"""KiCAD project management tools.

Tools:
    create_project       — generate a blank KiCAD project + mark active
    set_project          — mark an existing project as active
    get_project_state    — summary of active project (counts, paths)
    list_components      — list symbols in the active schematic
"""

from __future__ import annotations

import logging
from typing import Literal
from pathlib import Path

from kicad_claude import state
from kicad_claude.tools.common import resolve_sheet_path
from kicad_claude.adapters import pcb_editor as pcb_ed
from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.utils.kicad_strings import normalize_name

logger = logging.getLogger("kicad-claude.tools.core.project")


def _resolve_project_dir_and_name(project_path: str) -> tuple[Path, str]:
    """Accept either a project directory or any of its .kicad_* files.

    Returns (directory, project_name). Raises FileNotFoundError or ValueError
    if the path is ambiguous or no project file is found.
    """
    p = Path(project_path).expanduser().resolve()
    if p.is_file():
        if p.suffix not in (".kicad_pro", ".kicad_sch", ".kicad_pcb"):
            raise ValueError(f"unsupported file extension: {p.suffix}")
        return p.parent, p.stem

    if not p.is_dir():
        raise FileNotFoundError(f"path not found: {p}")

    pros = sorted(p.glob("*.kicad_pro"))
    if not pros:
        raise FileNotFoundError(f"no .kicad_pro file in {p}")
    if len(pros) > 1:
        names = [x.name for x in pros]
        raise ValueError(
            f"multiple .kicad_pro files in {p}: {names}; pass the file path directly"
        )
    return p, pros[0].stem


def _summarize(proj: state.ActiveProject) -> dict:
    """Cheap counts of an active project: symbols, footprints, nets.

    Read with this project's own parsers. `kicad-skip` was used here until it
    turned out it cannot parse a KiCAD 10 footprint's text items, which made
    `set_project` raise on every board that had a footprint on it.
    """
    sch_tree = sch_io.parse_file(proj.sch_path)
    pcb_tree = sch_io.parse_file(proj.pcb_path)
    return {
        "path": str(proj.path),
        "name": proj.name,
        "files": {
            "pro": str(proj.pro_path),
            "sch": str(proj.sch_path),
            "pcb": str(proj.pcb_path),
        },
        "symbols": sum(1 for _ in ed.iter_instance_symbols(sch_tree)),
        "footprints": sum(1 for _ in pcb_ed.iter_footprints(pcb_tree)),
        "nets": len(pcb_ed.list_nets(pcb_tree)),
        "formats": {
            "sch_version": _format_version(sch_tree),
            "pcb_version": _format_version(pcb_tree),
            # Which spelling this board uses for a net. KiCAD 10 dropped the
            # numbered table; saying so here stops a reader concluding that a
            # board whose pads carry `(net "GND")` has lost its nets.
            "pcb_net_storage": (
                "indexed table + (net <index> \"<name>\") — board format 9 and earlier"
                if pcb_ed.has_net_table(pcb_tree)
                else "name on each item, (net \"<name>\"), no net table — KiCAD 10"
            ),
        },
    }


def _format_version(tree: list) -> int | None:
    """The file's `(version N)` — the format KiCAD wrote, not the app version."""
    node = sch_io.find_child(tree, "version")
    if node is None or len(node) < 2:
        return None
    try:
        return int(node[1])
    except (TypeError, ValueError):
        return None


def _component_dict(sym: list) -> dict:
    """Best-effort extraction of a symbol's identity. Tolerant of missing fields."""
    reference = ed.get_symbol_property(sym, "Reference") or "?"
    value = ed.get_symbol_property(sym, "Value") or ""
    lib_id_node = sch_io.find_child(sym, "lib_id")
    lib_id = str(lib_id_node[1]) if lib_id_node and len(lib_id_node) > 1 else ""
    at = sch_io.find_child(sym, "at") or []
    x = float(at[1]) if len(at) > 1 else 0.0
    y = float(at[2]) if len(at) > 2 else 0.0
    rotation = float(at[3]) if len(at) > 3 else 0.0
    return {
        # Shown to the caller, so decode KiCAD's `{brace}` escapes.
        "reference": normalize_name(reference) if isinstance(reference, str) else reference,
        "value": normalize_name(value) if isinstance(value, str) else value,
        "lib_id": lib_id,
        "position_mm": [x, y],
        "rotation": rotation,
    }


def register(mcp) -> None:
    """Register the tools."""

    @mcp.tool()
    def create_project(path: str, name: str) -> dict:
        """Create a blank project `name` in directory `path` and make it active.

        Refuses if any of the .kicad_pro/.kicad_sch/.kicad_pcb files exists.
        """
        target = Path(path).expanduser().resolve()
        files = write_blank_project(target, name)
        proj = state.set_active(target, name)
        logger.info("created project %s at %s", name, target)
        return {
            "path": str(target),
            "name": name,
            "files": {k: str(v) for k, v in files.items()},
            "active": True,
            **_summarize(proj),
        }

    @mcp.tool()
    def set_project(project_path: str) -> dict:
        """Make an existing project active. Path: its directory or any project file."""
        directory, name = _resolve_project_dir_and_name(project_path)
        proj = state.set_active(directory, name)
        logger.info("active project: %s at %s", name, directory)
        return {"active": True, **_summarize(proj)}

    @mcp.tool()
    def clear_stale_lock() -> dict:
        """Delete KiCAD `.lck` files left by a crash, when provably stale.

        Stale = written by this user on this host and no KiCAD GUI running.
        Other locks are returned in `kept`, untouched. Writes refuse while any
        lock exists.
        """
        from kicad_claude.adapters import safe_write

        proj = state.get_active()
        removed: list = []
        kept: dict[str, dict] = {}
        seen: set[str] = set()
        for f in sorted(proj.path.glob("*.kicad_sch")) + sorted(proj.path.glob("*.kicad_pcb")):
            res = safe_write.clear_stale_locks(f)
            for st in res["removed"]:
                if st["path"] not in seen:
                    seen.add(st["path"])
                    removed.append(st)
            for st in res["kept"]:
                kept.setdefault(st["path"], st)
        return {
            "removed": removed,
            "kept": list(kept.values()),
            "locked": bool(kept),
        }

    @mcp.tool()
    def get_project_state() -> dict:
        """Active project: paths, symbol/footprint/net counts, file format versions."""
        return _summarize(state.get_active())

    @mcp.tool()
    def list_components(
        scope: Literal["active", "all"] = "active", sheet: str = ""
    ) -> list[dict]:
        """Schematic symbols: reference, value, lib_id, position_mm, rotation, sheet.

        `scope`: the active sheet, or the whole hierarchy. `sheet`: one sheet
        ("root", filename, or sheet name) without switching; not with "all".
        """
        if scope not in ("active", "all"):
            raise ValueError(f"scope must be 'active' or 'all' (got {scope!r})")
        if sheet and scope == "all":
            raise ValueError("pass either sheet= or scope='all', not both")
        proj = state.get_active()
        if sheet:
            paths = [resolve_sheet_path(proj, sheet)]
        elif scope == "active":
            paths = [state.get_active_sheet_path()]
        else:
            paths = ed.hierarchy_sch_paths(proj.sch_path)
        out: list[dict] = []
        for path in paths:
            tree = sch_io.parse_file(path)
            sheet_name = path.name
            for symbol in ed.iter_instance_symbols(tree):
                entry = _component_dict(symbol)
                entry["sheet"] = sheet_name
                out.append(entry)
        return out
